package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"time"
)

const crdResource = "poddiagnosticrequests"

// OperatorConfig configures the CR reconcile loop.
type OperatorConfig struct {
	KubeBin   string         // kubectl | oc
	Namespace string         // "" => all namespaces
	Analyzer  AnalyzerConfig // path to the poddebugger CLI + timeout
}

// --- PodDiagnosticRequest custom resource ----------------------------------

type pdrSpec struct {
	PodName           string            `json:"podName"`
	Namespace         string            `json:"namespace"`
	Container         string            `json:"container"`
	Deep              bool              `json:"deep"`
	RemediationMode   string            `json:"remediationMode"`
	RemediationAction string            `json:"remediationAction"`
	RemediationParams map[string]string `json:"remediationParams"`
	MaxAutoRisk       string            `json:"maxAutoRisk"`
	AllowedActions    []string          `json:"allowedActions"`
	Approved          bool              `json:"approved"`
}

type pdrStatus struct {
	Phase               string               `json:"phase,omitempty"`
	Summary             string               `json:"summary,omitempty"`
	RootCause           string               `json:"rootCause,omitempty"`
	Confidence          float64              `json:"confidence,omitempty"`
	SuggestedFixes      []Fix                `json:"suggestedFixes,omitempty"`
	ProposedRemediation *ProposedRemediation `json:"proposedRemediation,omitempty"`
	Remediation         *RemediationResult   `json:"remediation,omitempty"`
	ObservedTime        string               `json:"observedTime,omitempty"`
	Message             string               `json:"message,omitempty"`
}

// PodDiagnosticRequest mirrors the CRD defined in deploy/crd.yaml.
type PodDiagnosticRequest struct {
	Kind     string `json:"kind"`
	Metadata struct {
		Name      string `json:"name"`
		Namespace string `json:"namespace"`
	} `json:"metadata"`
	Spec   pdrSpec                `json:"spec"`
	Status pdrStatus              `json:"status"`
	Items  []PodDiagnosticRequest `json:"items"` // populated when Kind == "List"
}

// targetNamespace is where the pod under diagnosis lives.
func (cr PodDiagnosticRequest) targetNamespace() string {
	if cr.Spec.Namespace != "" {
		return cr.Spec.Namespace
	}
	return cr.Metadata.Namespace
}

func (cr PodDiagnosticRequest) mode() string {
	if cr.Spec.RemediationMode == "" {
		return "SuggestOnly"
	}
	return cr.Spec.RemediationMode
}

// remediationAction returns the manual override action set in the spec.
// May be empty — callers should fall back to the proposal (effectiveAction).
func (cr PodDiagnosticRequest) remediationAction() string {
	return cr.Spec.RemediationAction
}

// maxAutoRisk is the risk tier ceiling for AutoRemediate (HLD §12.5).
// Defaults to "low" — only restart/scale auto-apply.
func (cr PodDiagnosticRequest) maxAutoRisk() string {
	if cr.Spec.MaxAutoRisk == "" {
		return "low"
	}
	return cr.Spec.MaxAutoRisk
}

// effectiveAction resolves the action+params the operator should execute.
// A non-empty spec.remediationAction wins (manual override); otherwise the
// validated proposal from .status.proposedRemediation is used. Returns
// ("", nil) when there is nothing to apply.
func (cr PodDiagnosticRequest) effectiveAction() (string, map[string]string) {
	if cr.Spec.RemediationAction != "" {
		return cr.Spec.RemediationAction, cr.Spec.RemediationParams
	}
	p := cr.Status.ProposedRemediation
	if p == nil || !p.Validated || p.Action == "" || p.Action == "none" {
		return "", nil
	}
	params := make(map[string]string, len(p.Params))
	for k, v := range p.Params {
		params[k] = fmt.Sprintf("%v", v)
	}
	return p.Action, params
}

// canAutoApply returns (ok, reason) for an AutoRemediate decision.
// Refuses any proposal whose risk exceeds maxAutoRisk or whose action is not
// in spec.allowedActions (if that list is set).
func (cr PodDiagnosticRequest) canAutoApply(p *ProposedRemediation) (bool, string) {
	if p == nil || !p.Validated || p.Action == "" || p.Action == "none" {
		return false, "no validated proposal to apply"
	}
	rank := map[string]int{"low": 0, "medium": 1, "high": 2}
	if rank[p.Risk] > rank[cr.maxAutoRisk()] {
		return false, fmt.Sprintf("risk %q exceeds maxAutoRisk %q", p.Risk, cr.maxAutoRisk())
	}
	if allowed := cr.Spec.AllowedActions; len(allowed) > 0 {
		for _, a := range allowed {
			if a == p.Action {
				return true, ""
			}
		}
		return false, fmt.Sprintf("action %q not in spec.allowedActions", p.Action)
	}
	return true, ""
}

// --- reconcile loop --------------------------------------------------------

// RunOperator watches PodDiagnosticRequest CRs and reconciles each one.
func RunOperator(ctx context.Context, cfg OperatorConfig) error {
	args := []string{"get", crdResource, "--watch", "-o", "json"}
	if cfg.Namespace != "" {
		args = append(args, "-n", cfg.Namespace)
	} else {
		args = append(args, "--all-namespaces")
	}
	cmd := exec.CommandContext(ctx, cfg.KubeBin, args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("starting %s watch: %w", cfg.KubeBin, err)
	}

	// processed guards against re-reconciling the same transition when the
	// watch re-emits a CR (e.g. after our own status patch).
	processed := make(map[string]bool)
	dec := json.NewDecoder(stdout)
	for {
		var obj PodDiagnosticRequest
		if err := dec.Decode(&obj); err != nil {
			_ = cmd.Wait()
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return err
		}
		var crs []PodDiagnosticRequest
		if obj.Kind == "List" {
			crs = obj.Items
		} else if obj.Metadata.Name != "" {
			crs = []PodDiagnosticRequest{obj}
		}
		for _, cr := range crs {
			reconcile(ctx, cfg, cr, processed)
		}
	}
}

// reconcile drives one CR toward its desired state.
func reconcile(ctx context.Context, cfg OperatorConfig, cr PodDiagnosticRequest, processed map[string]bool) {
	ns, name := cr.Metadata.Namespace, cr.Metadata.Name
	key := ns + "/" + name

	needAnalysis := cr.Status.Phase == "" && !processed[key]
	needRemediate := cr.Status.Phase == "AwaitingApproval" &&
		cr.Spec.Approved && !processed[key+":remediate"]
	if !needAnalysis && !needRemediate {
		return
	}

	if needAnalysis {
		processed[key] = true
		analyzeAndMaybeRemediate(ctx, cfg, cr)
		return
	}

	processed[key+":remediate"] = true
	action, params := cr.effectiveAction()
	if action == "" {
		log.Printf("reconcile %s: approved but no actionable proposal — set spec.remediationAction explicitly", key)
		patchStatus(ctx, cfg, ns, name, pdrStatus{
			Phase:        "Failed",
			Message:      "approved but no actionable proposal — set spec.remediationAction (and spec.remediationParams) explicitly",
			ObservedTime: now(),
		})
		return
	}
	log.Printf("reconcile %s: approved — running %s remediation", key, action)
	st := pdrStatus{ObservedTime: now()}
	rem, err := RunRemediation(ctx, cfg.Analyzer, cr.Spec.PodName, cr.targetNamespace(), action, params)
	st.Remediation = &rem
	if err != nil {
		st.Phase, st.Message = "Failed", rem.Result
	} else {
		st.Phase = "Remediated"
	}
	patchStatus(ctx, cfg, ns, name, st)
}

// analyzeAndMaybeRemediate runs the analysis and applies the guardrail mode.
func analyzeAndMaybeRemediate(ctx context.Context, cfg OperatorConfig, cr PodDiagnosticRequest) {
	ns, name := cr.Metadata.Namespace, cr.Metadata.Name
	log.Printf("reconcile %s/%s: analyzing pod %s/%s (mode %s)",
		ns, name, cr.targetNamespace(), cr.Spec.PodName, cr.mode())

	ev := CrashEvent{
		Platform:  "kubernetes",
		Target:    cr.Spec.PodName,
		Namespace: cr.targetNamespace(),
		Container: cr.Spec.Container,
		Deep:      cr.Spec.Deep,
	}
	st := pdrStatus{ObservedTime: now()}

	// Skip the extra Remediator LLM call when nothing will be applied.
	wantFix := cr.mode() != "SuggestOnly"
	_, diag, err := RunAnalysis(ctx, cfg.Analyzer, ev, wantFix)
	if err != nil {
		st.Phase, st.Message = "Failed", err.Error()
		patchStatus(ctx, cfg, ns, name, st)
		return
	}
	st.Summary = diag.Summary
	st.RootCause = diag.RootCause
	st.Confidence = diag.Confidence
	st.SuggestedFixes = diag.SuggestedFixes
	st.ProposedRemediation = diag.ProposedRemediation

	// Honor an explicit manual override over the model's proposal; otherwise
	// thread the validated proposal back into cr.Status so effectiveAction()
	// can read it.
	cr.Status.ProposedRemediation = diag.ProposedRemediation

	switch cr.mode() {
	case "AutoRemediate":
		st.Phase, st.Message, st.Remediation = applyUnderAutoRemediate(ctx, cfg, cr)
	case "ApproveRequired":
		st.Phase = "AwaitingApproval"
		st.Message = approveRequiredMessage(cr)
	default: // SuggestOnly
		st.Phase = "Complete"
	}
	patchStatus(ctx, cfg, ns, name, st)
}

// applyUnderAutoRemediate decides whether to apply now or defer for human
// approval under AutoRemediate (HLD §12.5 + §17 — Phase 12 policies).
//
// Precedence ladder, top wins (HLD §17.2):
//  1. Explicit spec.allowedActions on the CR.
//  2. PodDebuggerApprovalPolicy rules (deny / requires-approval / allow).
//  3. Default behavior: canAutoApply (risk gate + spec.allowedActions).
//
// A policy `allow` decision bypasses the risk gate — the platform admin
// declared this action OK; the CR owner's maxAutoRisk is informational
// at that point.
func applyUnderAutoRemediate(ctx context.Context, cfg OperatorConfig, cr PodDiagnosticRequest) (phase, msg string, rem *RemediationResult) {
	action, params := cr.effectiveAction()
	if action == "" {
		return "AwaitingApproval",
			"AutoRemediate: no actionable proposal — set spec.remediationAction explicitly to apply manually",
			nil
	}

	// Step 1 — explicit spec.allowedActions on the CR (opt-out). If the
	// user manually pinned remediationAction, the allowedActions list must
	// still permit it.
	if cr.Spec.RemediationAction != "" {
		if allowed := cr.Spec.AllowedActions; len(allowed) > 0 {
			permitted := false
			for _, a := range allowed {
				if a == action {
					permitted = true
					break
				}
			}
			if !permitted {
				return "AwaitingApproval",
					fmt.Sprintf("AutoRemediate refused: spec.remediationAction %q not in spec.allowedActions", action),
					nil
			}
		}
	}

	// Step 2 — cluster-wide PodDebuggerApprovalPolicy overlay (Phase 12).
	// Degrades silently if the CRD isn't installed (LoadApprovalPolicies
	// returns nil, nil in that case).
	policyDecision, divergence := consultApprovalPolicies(ctx, cfg, cr, action)
	if policyDecision == PolicyDeny {
		log.Printf("auto-remediate refused by ApprovalPolicy: %s", action)
		return "AwaitingApproval",
			fmt.Sprintf("AutoRemediate refused by PodDebuggerApprovalPolicy: action %q is denied cluster-wide. Set spec.approved=true to apply anyway.", action),
			nil
	}
	if policyDecision == PolicyRequiresApproval {
		log.Printf("auto-remediate downgraded to ApproveRequired by policy: %s", action)
		return "AwaitingApproval",
			fmt.Sprintf("AutoRemediate downgraded by PodDebuggerApprovalPolicy: action %q requires approval. Set spec.approved=true to apply.", action),
			nil
	}

	// Step 3 — default risk-tier gate (Phase 7C). A policy `allow` skips
	// this; the admin explicitly cleared the action.
	if policyDecision != PolicyAllow && cr.Spec.RemediationAction == "" {
		if ok, why := cr.canAutoApply(cr.Status.ProposedRemediation); !ok {
			log.Printf("auto-remediate refused: %s", why)
			return "AwaitingApproval",
				fmt.Sprintf("AutoRemediate refused: %s — set spec.approved=true to apply anyway", why),
				nil
		}
	}

	result, rerr := RunRemediation(ctx, cfg.Analyzer, cr.Spec.PodName, cr.targetNamespace(), action, params)
	rem = &result
	if rerr != nil {
		return "Failed", result.Result, rem
	}
	if divergence != "" {
		// Successful remediation but note the policy disagreement so the
		// CR owner sees that spec.allowedActions overrode a policy.
		return "Remediated", divergence, rem
	}
	return "Remediated", "", rem
}

// consultApprovalPolicies fetches policies, looks up the target namespace's
// labels, evaluates rules, and returns (decision, divergenceMessage).
// `divergenceMessage` is set when a policy would have denied an action that
// the CR's explicit spec.allowedActions permitted — informational only,
// surfaced on .status.message for audit (Stage 12B).
func consultApprovalPolicies(ctx context.Context, cfg OperatorConfig,
	cr PodDiagnosticRequest, action string) (PolicyDecision, string) {
	policies, err := LoadApprovalPolicies(ctx, cfg.KubeBin)
	if err != nil {
		log.Printf("approval policies: %v (continuing without policy check)", err)
		return "", ""
	}
	if len(policies) == 0 {
		return "", ""
	}
	labels, lerr := GetNamespaceLabels(ctx, cfg.KubeBin, cr.targetNamespace())
	if lerr != nil {
		log.Printf("namespace labels for %q: %v (treating as unlabeled)",
			cr.targetNamespace(), lerr)
	}
	decision := EvaluatePolicies(policies, labels, action)
	if decision == "" {
		return "", ""
	}
	// Stage 12B — divergence reporting. When the CR's allowedActions
	// permits the action but a policy would have denied it, surface the
	// disagreement so a human can investigate.
	var divergence string
	if decision == PolicyDeny && actionInList(action, cr.Spec.AllowedActions) {
		divergence = fmt.Sprintf(
			"NOTE: PodDebuggerApprovalPolicy denied action %q for this namespace, "+
				"but spec.allowedActions overrode it. Review the policy or remove "+
				"the action from spec.allowedActions.", action)
		// Override: spec.allowedActions wins per precedence ladder. Strip
		// the deny so the caller proceeds.
		return "", divergence
	}
	return decision, divergence
}

func actionInList(action string, allowed []string) bool {
	for _, a := range allowed {
		if a == action {
			return true
		}
	}
	return false
}

func approveRequiredMessage(cr PodDiagnosticRequest) string {
	action, _ := cr.effectiveAction()
	if action == "" {
		return "diagnosis ready — set spec.remediationAction (and approved=true) to remediate"
	}
	return fmt.Sprintf("diagnosis ready — proposed action %q; set spec.approved=true to apply", action)
}

// patchStatus writes a merge patch to the CR's /status subresource.
func patchStatus(ctx context.Context, cfg OperatorConfig, ns, name string, st pdrStatus) {
	payload, err := json.Marshal(map[string]pdrStatus{"status": st})
	if err != nil {
		log.Printf("patch %s/%s: marshal failed: %v", ns, name, err)
		return
	}
	cctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, cfg.KubeBin, "patch", crdResource, name,
		"-n", ns, "--subresource=status", "--type=merge", "-p", string(payload))
	if out, err := cmd.CombinedOutput(); err != nil {
		log.Printf("patch %s/%s failed: %v: %s", ns, name, err, string(out))
		return
	}
	log.Printf("reconcile %s/%s: status -> %s", ns, name, st.Phase)
}

func now() string { return time.Now().UTC().Format(time.RFC3339) }
