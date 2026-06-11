package main

import (
	"context"
	"encoding/json"
)

// CrashEvent is a detected workload failure worth analyzing.
type CrashEvent struct {
	Platform  string // podman | kubernetes
	Target    string // container or pod name
	Namespace string // kubernetes only
	Reason    string // died | CrashLoopBackOff | OOMKilled | ...
	Detail    string // e.g. "exit code 137", container name
	Container string // specific container to focus on (operator mode)
	Deep      bool   // request deep inspection (operator mode)
}

// Key uniquely identifies a target for cooldown bookkeeping.
func (e CrashEvent) Key() string {
	if e.Namespace != "" {
		return e.Platform + "/" + e.Namespace + "/" + e.Target
	}
	return e.Platform + "/" + e.Target
}

// Label is the human-readable target name used in log lines.
func (e CrashEvent) Label() string {
	if e.Namespace != "" {
		return e.Namespace + "/" + e.Target
	}
	return e.Target
}

// Diagnosis mirrors the JSON emitted by `poddebugger analyze --json`
// (the Python Diagnosis dataclass).
type Diagnosis struct {
	Summary             string   `json:"summary"`
	RootCause           string   `json:"root_cause"`
	Confidence          float64  `json:"confidence"`
	Evidence            []string `json:"evidence"`
	SuggestedFixes      []Fix    `json:"suggested_fixes"`
	NeedsDeepInspection bool     `json:"needs_deep_inspection"`
	// ProposedRemediation is set by `analyze --fix` (Phase 7B). The CLI
	// outputs the snake_case key; the operator stores it on .status as the
	// camelCase JSON in pdrStatus.ProposedRemediation.
	ProposedRemediation *ProposedRemediation `json:"proposed_remediation,omitempty"`
}

// Fix is one suggested remediation.
type Fix struct {
	Action    string `json:"action"`
	Rationale string `json:"rationale"`
	Risk      string `json:"risk"`
}

// ProposedRemediation mirrors a Remediator agent proposal (HLD §12.3),
// validated against the catalog by the Python engine before reaching here.
// All fields are optional — a {action: "none"} proposal has only Action and
// Reason populated; a rejected proposal carries ValidationError.
type ProposedRemediation struct {
	Action          string         `json:"action"`
	Params          map[string]any `json:"params,omitempty"`
	Risk            string         `json:"risk,omitempty"`
	Rationale       string         `json:"rationale,omitempty"`
	ExpectedEffect  string         `json:"expected_effect,omitempty"`
	Confidence      float64        `json:"confidence,omitempty"`
	Reversal        map[string]any `json:"reversal,omitempty"`
	Validated       bool           `json:"validated,omitempty"`
	Reason          string         `json:"reason,omitempty"`
	ValidationError string         `json:"validation_error,omitempty"`
}

// The analyzer CLI emits snake_case JSON; the CRD's .status convention is
// camelCase. The three types below therefore decode snake_case (struct
// tags) but encode camelCase (custom MarshalJSON), so the same value can
// be read from the CLI and written to .status without a conversion layer.
// Found in live-cluster validation: without this, the status patch carried
// expected_effect / saved_to / waited_seconds, which the structural schema
// pruned (or, for object-valued params, rejected outright).

// MarshalJSON emits the CRD's camelCase field names.
func (p ProposedRemediation) MarshalJSON() ([]byte, error) {
	m := map[string]any{"action": p.Action}
	if len(p.Params) > 0 {
		m["params"] = p.Params
	}
	if p.Risk != "" {
		m["risk"] = p.Risk
	}
	if p.Rationale != "" {
		m["rationale"] = p.Rationale
	}
	if p.ExpectedEffect != "" {
		m["expectedEffect"] = p.ExpectedEffect
	}
	if p.Confidence != 0 {
		m["confidence"] = p.Confidence
	}
	if len(p.Reversal) > 0 {
		m["reversal"] = p.Reversal
	}
	if p.Validated {
		m["validated"] = true
	}
	if p.Reason != "" {
		m["reason"] = p.Reason
	}
	if p.ValidationError != "" {
		m["validationError"] = p.ValidationError
	}
	return json.Marshal(m)
}

// MarshalJSON emits the CRD's camelCase field names.
func (r RemediationResult) MarshalJSON() ([]byte, error) {
	m := map[string]any{
		"action":   r.Action,
		"executed": r.Executed,
		"result":   r.Result,
	}
	if r.Verification != nil {
		m["verification"] = r.Verification
	}
	if r.SavedTo != "" {
		m["savedTo"] = r.SavedTo
	}
	return json.Marshal(m)
}

// MarshalJSON emits the CRD's camelCase field names.
func (v Verification) MarshalJSON() ([]byte, error) {
	m := map[string]any{"outcome": v.Outcome}
	if v.Reason != "" {
		m["reason"] = v.Reason
	}
	if v.WaitedSeconds != 0 {
		m["waitedSeconds"] = v.WaitedSeconds
	}
	return json.Marshal(m)
}

// --- Phase 12 — PodDebuggerApprovalPolicy ---------------------------------

// ApprovalPolicy is the cluster-scoped overlay that lets platform admins
// pre-approve / refuse / downgrade catalog actions across many
// PodDiagnosticRequests at once. Operator equivalent of the CLI's Phase 11
// rules file. See HLD §17 for the precedence ladder.
type ApprovalPolicy struct {
	Kind     string `json:"kind"`
	Metadata struct {
		Name string `json:"name"`
	} `json:"metadata"`
	Spec  ApprovalPolicySpec `json:"spec"`
	Items []ApprovalPolicy   `json:"items"` // populated when Kind == "List"
}

// ApprovalPolicySpec mirrors the CRD's `.spec`.
type ApprovalPolicySpec struct {
	Scope ApprovalPolicyScope  `json:"scope"`
	Rules []ApprovalPolicyRule `json:"rules"`
}

// ApprovalPolicyScope is which PDRs the policy applies to.
type ApprovalPolicyScope struct {
	NamespaceSelector LabelSelector `json:"namespaceSelector"`
}

// LabelSelector — minimal subset of metav1.LabelSelector (matchLabels only).
type LabelSelector struct {
	MatchLabels map[string]string `json:"matchLabels"`
}

// ApprovalPolicyRule is one allow / deny / requires-approval rule.
type ApprovalPolicyRule struct {
	Kind     string `json:"kind"`     // "remediation" (only kind supported in v1)
	Action   string `json:"action"`   // catalog action name
	Decision string `json:"decision"` // allow | deny | requires-approval
	Expires  string `json:"expires"`  // optional ISO date "YYYY-MM-DD"
}

// PolicyDecision is the resolved verdict from consulting all matching
// policies for one (target namespace, action) pair. Empty when no policy
// matched.
type PolicyDecision string

const (
	PolicyAllow            PolicyDecision = "allow"
	PolicyDeny             PolicyDecision = "deny"
	PolicyRequiresApproval PolicyDecision = "requires-approval"
)

// RemediationResult mirrors the JSON from `poddebugger remediate --json`.
type RemediationResult struct {
	Action       string        `json:"action"`
	Executed     bool          `json:"executed"`
	Result       string        `json:"result"`
	Verification *Verification `json:"verification,omitempty"`
	SavedTo      string        `json:"saved_to,omitempty"`
}

// Verification mirrors the Phase 7D post-remediation re-check
// (HLD §12.7). “Outcome“ is one of: recovered | still-failing | unknown |
// skipped.
type Verification struct {
	Outcome       string         `json:"outcome"`
	Reason        string         `json:"reason,omitempty"`
	WaitedSeconds int            `json:"waited_seconds,omitempty"`
	Baseline      map[string]any `json:"baseline,omitempty"`
	Observed      map[string]any `json:"observed,omitempty"`
}

// Watcher streams crash events into out until the context is cancelled.
type Watcher interface {
	Watch(ctx context.Context, out chan<- CrashEvent) error
}
