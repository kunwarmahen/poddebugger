package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"sort"
	"strings"
	"time"
)

// AnalyzerConfig describes how to invoke the poddebugger CLI.
type AnalyzerConfig struct {
	Path    string        // path to the `poddebugger` executable
	NoLLM   bool          // pass --no-llm (collect context only, no diagnosis)
	Timeout time.Duration // max time for one analysis
}

// RunAnalysis invokes `poddebugger analyze ... --json` for a crash event.
//
// With NoLLM the CLI prints collected context (not JSON), so diag is nil and
// raw holds that text. Otherwise raw is the JSON and diag is the parsed result.
// When withFix is true, `--fix` is also passed — the Remediator agent runs
// after the verdict and the diagnosis carries .ProposedRemediation (Phase 7B).
func RunAnalysis(ctx context.Context, cfg AnalyzerConfig, ev CrashEvent, withFix bool) (raw string, diag *Diagnosis, err error) {
	args := []string{"analyze", ev.Target, "--platform", ev.Platform, "--json"}
	if ev.Namespace != "" {
		args = append(args, "-n", ev.Namespace)
	}
	if ev.Container != "" {
		args = append(args, "--container", ev.Container)
	}
	if ev.Deep {
		args = append(args, "--deep")
	}
	if withFix {
		args = append(args, "--fix")
	}
	if cfg.NoLLM {
		args = append(args, "--no-llm")
	}

	cctx, cancel := context.WithTimeout(ctx, cfg.Timeout)
	defer cancel()

	cmd := exec.CommandContext(cctx, cfg.Path, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return "", nil, fmt.Errorf("%s", msg)
	}

	raw = strings.TrimSpace(stdout.String())
	if cfg.NoLLM {
		return raw, nil, nil
	}
	var d Diagnosis
	if err := json.Unmarshal([]byte(raw), &d); err != nil {
		return raw, nil, fmt.Errorf("could not parse diagnosis JSON: %w", err)
	}
	return raw, &d, nil
}

// RunRemediation invokes `poddebugger remediate ... --confirm --json` and
// returns the parsed result. The CLI exits non-zero when the action did not
// execute; that surfaces here as a non-nil error alongside the result.
// Each entry in params is appended as `--param key=value` (Phase 7A).
func RunRemediation(ctx context.Context, cfg AnalyzerConfig, target, namespace, action string, params map[string]string) (RemediationResult, error) {
	args := []string{
		"remediate", target,
		"--platform", "kubernetes",
		"--action", action,
		"--confirm", "--json",
	}
	if namespace != "" {
		args = append(args, "-n", namespace)
	}
	// Stable ordering — k8s patches and tests both prefer it.
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		args = append(args, "--param", fmt.Sprintf("%s=%s", k, params[k]))
	}

	cctx, cancel := context.WithTimeout(ctx, cfg.Timeout)
	defer cancel()

	cmd := exec.CommandContext(cctx, cfg.Path, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	runErr := cmd.Run()

	var res RemediationResult
	if raw := strings.TrimSpace(stdout.String()); raw != "" {
		_ = json.Unmarshal([]byte(raw), &res)
	}
	if !res.Executed {
		if res.Result == "" {
			res.Result = strings.TrimSpace(stderr.String())
		}
		if res.Result == "" && runErr != nil {
			res.Result = runErr.Error()
		}
		return res, fmt.Errorf("remediation did not execute: %s", res.Result)
	}
	return res, nil
}
