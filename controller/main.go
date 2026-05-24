// Command poddebugger-watch is the Phase 4 control plane: it watches a
// container runtime for crashes and runs the poddebugger analyzer on each one.
//
// It is a thin orchestrator — crash detection here, diagnosis delegated to the
// `poddebugger` CLI (the Python "brain"). Contract: `poddebugger analyze
// <target> --json`.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"
)

func main() {
	platform := flag.String("platform", "podman", "container runtime to watch: podman | kubernetes")
	analyzer := flag.String("analyzer", "poddebugger", "path to the poddebugger CLI")
	kubeBin := flag.String("kubectl", "kubectl", "kubectl or oc binary (kubernetes platform)")
	namespace := flag.String("namespace", "", "kubernetes namespace to watch (default: all)")
	cooldown := flag.Duration("cooldown", 5*time.Minute, "per-target re-analysis cooldown")
	timeout := flag.Duration("timeout", 3*time.Minute, "max time for one analysis")
	noLLM := flag.Bool("no-llm", false, "pass --no-llm to the analyzer (collect context, no diagnosis)")
	operator := flag.Bool("operator", false, "operator mode: reconcile PodDiagnosticRequest CRs instead of watching for crashes")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// Operator mode: reconcile PodDiagnosticRequest custom resources.
	if *operator {
		log.Printf("poddebugger-watch: operator mode — reconciling %s", crdResource)
		err := RunOperator(ctx, OperatorConfig{
			KubeBin:   *kubeBin,
			Namespace: *namespace,
			Analyzer:  AnalyzerConfig{Path: *analyzer, Timeout: *timeout},
		})
		if err != nil && ctx.Err() == nil {
			log.Fatalf("operator: %v", err)
		}
		log.Print("shutting down")
		return
	}

	// Watcher mode: tail the runtime for crashes.
	var watcher Watcher
	switch *platform {
	case "podman":
		watcher = PodmanWatcher{Binary: "podman"}
	case "kubernetes", "openshift":
		watcher = KubernetesWatcher{Binary: *kubeBin, Namespace: *namespace}
	default:
		log.Fatalf("unknown platform %q (expected podman or kubernetes)", *platform)
	}

	acfg := AnalyzerConfig{Path: *analyzer, NoLLM: *noLLM, Timeout: *timeout}
	deb := newDebouncer(*cooldown)

	events := make(chan CrashEvent, 16)
	go func() {
		defer close(events)
		if err := watcher.Watch(ctx, events); err != nil && ctx.Err() == nil {
			log.Printf("watcher stopped: %v", err)
		}
	}()

	log.Printf("poddebugger-watch: watching %s for crashes (cooldown %s, analyzer %q)",
		*platform, *cooldown, *analyzer)

	for {
		select {
		case <-ctx.Done():
			log.Print("shutting down")
			return
		case ev, ok := <-events:
			if !ok {
				return
			}
			if !deb.allow(ev.Key()) {
				log.Printf("crash on %s [%s] — within cooldown, skipping", ev.Label(), ev.Reason)
				continue
			}
			handle(ctx, acfg, ev)
		}
	}
}

// handle analyzes one crash event and prints the result.
func handle(ctx context.Context, cfg AnalyzerConfig, ev CrashEvent) {
	log.Printf("crash detected: %s [%s] %s — analyzing...", ev.Label(), ev.Reason, ev.Detail)
	// The watcher path stays read-only — the operator drives remediation.
	raw, diag, err := RunAnalysis(ctx, cfg, ev, false)
	if err != nil {
		log.Printf("analysis failed for %s: %v", ev.Label(), err)
		return
	}
	if diag == nil { // --no-llm: raw collected context, not a diagnosis
		fmt.Printf("\n----- collected context: %s -----\n%s\n\n", ev.Label(), raw)
		return
	}
	printAlert(ev, *diag)
}

// printAlert renders a diagnosis as a crash-alert block.
func printAlert(ev CrashEvent, d Diagnosis) {
	bar := strings.Repeat("=", 64)
	var b strings.Builder
	fmt.Fprintf(&b, "\n%s\n  CRASH ALERT — %s\n%s\n", bar, ev.Label(), bar)
	fmt.Fprintf(&b, "trigger:    %s (%s)\n", ev.Reason, ev.Detail)
	fmt.Fprintf(&b, "summary:    %s\n", d.Summary)
	fmt.Fprintf(&b, "confidence: %.0f%%\n", d.Confidence*100)
	fmt.Fprintf(&b, "root cause: %s\n", d.RootCause)
	if len(d.SuggestedFixes) > 0 {
		b.WriteString("suggested fixes:\n")
		for i, f := range d.SuggestedFixes {
			fmt.Fprintf(&b, "  %d. [%s risk] %s\n", i+1, f.Risk, f.Action)
		}
	}
	if d.NeedsDeepInspection {
		b.WriteString("note:       deeper inspection recommended (re-run analyze with --deep)\n")
	}
	b.WriteString(bar + "\n")
	fmt.Print(b.String())
}

// debouncer enforces a per-target cooldown so a crash-looping workload is not
// re-analyzed on every restart.
type debouncer struct {
	mu       sync.Mutex
	cooldown time.Duration
	last     map[string]time.Time
}

func newDebouncer(d time.Duration) *debouncer {
	return &debouncer{cooldown: d, last: make(map[string]time.Time)}
}

func (d *debouncer) allow(key string) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	now := time.Now()
	if t, ok := d.last[key]; ok && now.Sub(t) < d.cooldown {
		return false
	}
	d.last[key] = now
	return true
}
