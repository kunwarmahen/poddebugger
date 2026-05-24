// Phase 12 — PodDebuggerApprovalPolicy support.
//
// Fetches cluster-scoped PodDebuggerApprovalPolicy resources, evaluates them
// against the PodDiagnosticRequest being reconciled, and returns a single
// PolicyDecision (allow / deny / requires-approval / "") for a given action.
//
// Match semantics (see HLD §17.2):
//   - scope.namespaceSelector picks which PDR namespaces this policy applies
//     to. An empty selector matches every namespace.
//   - Rules match by (kind, action). The FIRST matching deny across all
//     active policies always wins. Otherwise the first matching allow /
//     requires-approval wins.
//   - Expired rules (expires < today) are skipped silently.
//
// The operator re-fetches policies per reconcile — they're cluster-scoped
// and small; a watch-cache is a future optimization.

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"time"
)

// LoadApprovalPolicies fetches every PodDebuggerApprovalPolicy from the
// cluster. Returns an empty slice (not an error) if the CRD isn't
// installed — Phase 12 must degrade gracefully on a Phase-11-era cluster.
func LoadApprovalPolicies(ctx context.Context, kubeBin string) ([]ApprovalPolicy, error) {
	cctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, kubeBin,
		"get", "poddebuggerapprovalpolicies", "-o", "json")
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		// `the server doesn't have a resource type` → CRD not installed.
		// `No resources found` is a clean rc=0; non-zero with that text =
		// rare, but treat the same.
		stderrStr := stderr.String()
		if containsAny(stderrStr,
			"doesn't have a resource type",
			"could not find the requested resource",
			"the server could not find") {
			return nil, nil
		}
		return nil, fmt.Errorf("listing approval policies: %s", stderrStr)
	}
	var list ApprovalPolicy
	if err := json.Unmarshal(stdout.Bytes(), &list); err != nil {
		return nil, fmt.Errorf("decoding approval policies: %w", err)
	}
	if list.Kind == "List" {
		return list.Items, nil
	}
	// Single object (unusual for `-o json`) — wrap it.
	if list.Metadata.Name != "" {
		return []ApprovalPolicy{list}, nil
	}
	return nil, nil
}

// GetNamespaceLabels reads `kubectl get namespace <ns> -o jsonpath=...` to
// retrieve the labels we need for namespaceSelector matching. Returns an
// empty map if the namespace cannot be read (logged by the caller).
func GetNamespaceLabels(ctx context.Context, kubeBin, namespace string) (map[string]string, error) {
	if namespace == "" {
		return nil, nil
	}
	cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, kubeBin,
		"get", "namespace", namespace, "-o", "jsonpath={.metadata.labels}")
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("get namespace %q: %s", namespace, stderr.String())
	}
	raw := stdout.Bytes()
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil, nil
	}
	labels := map[string]string{}
	if err := json.Unmarshal(raw, &labels); err != nil {
		return nil, fmt.Errorf("decoding namespace labels: %w", err)
	}
	return labels, nil
}

// EvaluatePolicies returns the resolved PolicyDecision for `action` given
// the candidate policies and the target namespace's labels. Empty string
// means no policy matched. Deny always beats allow when both rules match.
func EvaluatePolicies(policies []ApprovalPolicy, namespaceLabels map[string]string,
	action string) PolicyDecision {
	var allow, requiresApproval PolicyDecision
	for _, p := range policies {
		if !scopeMatches(p.Spec.Scope, namespaceLabels) {
			continue
		}
		for _, rule := range p.Spec.Rules {
			if !ruleApplies(rule, action) {
				continue
			}
			switch PolicyDecision(rule.Decision) {
			case PolicyDeny:
				return PolicyDeny // deny is the last word — return now
			case PolicyAllow:
				if allow == "" {
					allow = PolicyAllow
				}
			case PolicyRequiresApproval:
				if requiresApproval == "" {
					requiresApproval = PolicyRequiresApproval
				}
			}
		}
	}
	// Order: deny (already returned) > requires-approval > allow.
	// requires-approval is stricter than allow — if anyone declared this
	// action gated, honor the gate.
	if requiresApproval != "" {
		return requiresApproval
	}
	return allow
}

// scopeMatches: empty selector matches everything; otherwise matchLabels
// must be a subset of the namespace's labels.
func scopeMatches(scope ApprovalPolicyScope, namespaceLabels map[string]string) bool {
	sel := scope.NamespaceSelector.MatchLabels
	if len(sel) == 0 {
		return true
	}
	for k, want := range sel {
		if got, ok := namespaceLabels[k]; !ok || got != want {
			return false
		}
	}
	return true
}

// ruleApplies: rule's kind must be "remediation" (Phase 12 scope) and the
// action must match (exact). Expired rules are skipped.
func ruleApplies(rule ApprovalPolicyRule, action string) bool {
	if rule.Kind != "" && rule.Kind != "remediation" {
		return false
	}
	if rule.Action != "" && rule.Action != action {
		return false
	}
	if ruleExpired(rule.Expires) {
		return false
	}
	return true
}

func ruleExpired(expires string) bool {
	if expires == "" {
		return false
	}
	exp, err := time.Parse("2006-01-02", expires)
	if err != nil {
		// Malformed expiry — treat as expired so a typo doesn't quietly
		// leave a stale rule active. The admin will see it on next list.
		return true
	}
	return time.Now().UTC().Truncate(24 * time.Hour).After(exp)
}

func containsAny(s string, substrs ...string) bool {
	for _, sub := range substrs {
		if sub != "" && bytesContains(s, sub) {
			return true
		}
	}
	return false
}

// Tiny helper to avoid an `import "strings"` just for one Contains call.
func bytesContains(haystack, needle string) bool {
	if len(needle) > len(haystack) {
		return false
	}
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}
