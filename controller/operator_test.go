package main

import (
	"encoding/json"
	"strings"
	"testing"
)

// TestPDRDecodeAndDefaults checks that a PodDiagnosticRequest as kubectl would
// emit it decodes correctly and the spec defaults resolve as designed.
func TestPDRDecodeAndDefaults(t *testing.T) {
	raw := `{
	  "kind": "PodDiagnosticRequest",
	  "metadata": {"name": "diagnose-foo", "namespace": "poddebugger"},
	  "spec": {"podName": "foo-7f8b9d", "namespace": "app-prod", "deep": true},
	  "status": {}
	}`
	var cr PodDiagnosticRequest
	if err := json.Unmarshal([]byte(raw), &cr); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if cr.Spec.PodName != "foo-7f8b9d" {
		t.Errorf("podName = %q", cr.Spec.PodName)
	}
	if !cr.Spec.Deep {
		t.Error("deep should be true")
	}
	if got := cr.targetNamespace(); got != "app-prod" {
		t.Errorf("targetNamespace = %q, want app-prod", got)
	}
	if got := cr.mode(); got != "SuggestOnly" {
		t.Errorf("mode default = %q, want SuggestOnly", got)
	}
	if got := cr.remediationAction(); got != "" {
		t.Errorf("remediationAction default = %q, want empty (use proposal)", got)
	}
	if got := cr.maxAutoRisk(); got != "low" {
		t.Errorf("maxAutoRisk default = %q, want low", got)
	}
}

func TestPDRTargetNamespaceFallback(t *testing.T) {
	var cr PodDiagnosticRequest
	cr.Metadata.Namespace = "poddebugger"
	if got := cr.targetNamespace(); got != "poddebugger" {
		t.Errorf("targetNamespace = %q, want fallback to metadata ns", got)
	}
}

func TestPDRExplicitMode(t *testing.T) {
	var cr PodDiagnosticRequest
	cr.Spec.RemediationMode = "AutoRemediate"
	if cr.mode() != "AutoRemediate" {
		t.Errorf("mode = %q, want AutoRemediate", cr.mode())
	}
}

// TestPatchStatusPayload verifies the merge-patch body shape: a single
// "status" key, omitempty fields dropped so a partial patch never clobbers
// an earlier diagnosis.
func TestPatchStatusPayload(t *testing.T) {
	st := pdrStatus{Phase: "Remediated", Remediation: &RemediationResult{
		Action: "restart", Executed: true, Result: "pod deleted",
	}}
	b, err := json.Marshal(map[string]pdrStatus{"status": st})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]map[string]any
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	status := got["status"]
	if status["phase"] != "Remediated" {
		t.Errorf("phase = %v", status["phase"])
	}
	if _, ok := status["summary"]; ok {
		t.Error("empty summary should be omitted (would clobber prior diagnosis)")
	}
}

// TestPDRDecodeNewSpecFields decodes a CR carrying the Phase 7C fields.
func TestPDRDecodeNewSpecFields(t *testing.T) {
	raw := `{
	  "kind": "PodDiagnosticRequest",
	  "metadata": {"name": "diagnose-bar", "namespace": "poddebugger"},
	  "spec": {
	    "podName": "bar-7f8",
	    "remediationMode": "AutoRemediate",
	    "remediationParams": {"replicas": "3"},
	    "maxAutoRisk": "medium",
	    "allowedActions": ["restart", "scale"]
	  },
	  "status": {}
	}`
	var cr PodDiagnosticRequest
	if err := json.Unmarshal([]byte(raw), &cr); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got := cr.Spec.RemediationParams["replicas"]; got != "3" {
		t.Errorf("remediationParams[replicas] = %q", got)
	}
	if got := cr.maxAutoRisk(); got != "medium" {
		t.Errorf("maxAutoRisk = %q, want medium", got)
	}
	if len(cr.Spec.AllowedActions) != 2 {
		t.Errorf("allowedActions = %v", cr.Spec.AllowedActions)
	}
}

// TestEffectiveActionPrefersOverride: an explicit spec.remediationAction wins
// over the model's proposal.
func TestEffectiveActionPrefersOverride(t *testing.T) {
	cr := PodDiagnosticRequest{}
	cr.Spec.RemediationAction = "scale"
	cr.Spec.RemediationParams = map[string]string{"replicas": "5"}
	cr.Status.ProposedRemediation = &ProposedRemediation{
		Action: "restart", Validated: true,
	}
	action, params := cr.effectiveAction()
	if action != "scale" {
		t.Errorf("action = %q, want scale (override wins)", action)
	}
	if params["replicas"] != "5" {
		t.Errorf("params = %v", params)
	}
}

// TestEffectiveActionUsesValidatedProposal: with no override, the validated
// proposal supplies action+params.
func TestEffectiveActionUsesValidatedProposal(t *testing.T) {
	cr := PodDiagnosticRequest{}
	cr.Status.ProposedRemediation = &ProposedRemediation{
		Action: "set-resources", Validated: true, Risk: "medium",
		Params: map[string]any{"container": "app", "memory_limit": "512Mi"},
	}
	action, params := cr.effectiveAction()
	if action != "set-resources" {
		t.Errorf("action = %q, want set-resources", action)
	}
	if params["container"] != "app" || params["memory_limit"] != "512Mi" {
		t.Errorf("params = %v", params)
	}
}

// TestEffectiveActionIgnoresUnvalidatedProposal: a rejected proposal is not
// treated as actionable.
func TestEffectiveActionIgnoresUnvalidatedProposal(t *testing.T) {
	cr := PodDiagnosticRequest{}
	cr.Status.ProposedRemediation = &ProposedRemediation{
		Action: "scale", Validated: false,
	}
	if action, _ := cr.effectiveAction(); action != "" {
		t.Errorf("action = %q, want empty for unvalidated proposal", action)
	}
}

// TestEffectiveActionNoneIsNotActionable: action="none" is not applied.
func TestEffectiveActionNoneIsNotActionable(t *testing.T) {
	cr := PodDiagnosticRequest{}
	cr.Status.ProposedRemediation = &ProposedRemediation{
		Action: "none", Validated: true, Reason: "code fix needed",
	}
	if action, _ := cr.effectiveAction(); action != "" {
		t.Errorf("action = %q, want empty for action=none", action)
	}
}

// TestCanAutoApplyRiskGate: a medium-risk proposal is refused under the
// default (low) maxAutoRisk.
func TestCanAutoApplyRiskGate(t *testing.T) {
	cr := PodDiagnosticRequest{}
	p := &ProposedRemediation{Action: "set-resources", Risk: "medium", Validated: true}
	if ok, _ := cr.canAutoApply(p); ok {
		t.Error("low maxAutoRisk should refuse a medium-risk proposal")
	}
	cr.Spec.MaxAutoRisk = "medium"
	if ok, why := cr.canAutoApply(p); !ok {
		t.Errorf("medium maxAutoRisk should accept a medium-risk proposal: %s", why)
	}
}

// TestCanAutoApplyAllowList: a proposal outside spec.allowedActions is refused
// even when its risk is below the ceiling.
func TestCanAutoApplyAllowList(t *testing.T) {
	cr := PodDiagnosticRequest{}
	cr.Spec.AllowedActions = []string{"restart"}
	p := &ProposedRemediation{Action: "scale", Risk: "low", Validated: true}
	if ok, why := cr.canAutoApply(p); ok || !strings.Contains(why, "scale") {
		t.Errorf("canAutoApply = %v, %q; want refusal mentioning scale", ok, why)
	}
}

// TestCanAutoApplyRejectsNilOrUnvalidated guards the safe-by-default path.
func TestCanAutoApplyRejectsNilOrUnvalidated(t *testing.T) {
	cr := PodDiagnosticRequest{}
	if ok, _ := cr.canAutoApply(nil); ok {
		t.Error("nil proposal must be refused")
	}
	if ok, _ := cr.canAutoApply(&ProposedRemediation{Action: "restart"}); ok {
		t.Error("unvalidated proposal must be refused")
	}
}

// TestPatchStatusCarriesProposal: a partial patch including the proposal
// round-trips without dropping it.
func TestPatchStatusCarriesProposal(t *testing.T) {
	st := pdrStatus{
		Phase: "AwaitingApproval",
		ProposedRemediation: &ProposedRemediation{
			Action: "scale", Risk: "low", Validated: true,
			Params: map[string]any{"replicas": "3"},
		},
	}
	b, err := json.Marshal(map[string]pdrStatus{"status": st})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !strings.Contains(string(b), `"proposedRemediation"`) {
		t.Errorf("missing proposedRemediation in patch: %s", string(b))
	}
	if !strings.Contains(string(b), `"action":"scale"`) {
		t.Errorf("missing scale action in patch: %s", string(b))
	}
}

// TestK8sCrashReason exercises the watcher's pod crash classifier.
func TestK8sCrashReason(t *testing.T) {
	waiting := func(reason string) k8sPod {
		var p k8sPod
		p.Status.ContainerStatuses = []k8sContainerStatus{{
			Name:  "app",
			State: k8sContainerState{Waiting: &k8sReason{Reason: reason}},
		}}
		return p
	}
	oom := func() k8sPod {
		var p k8sPod
		p.Status.ContainerStatuses = []k8sContainerStatus{{
			Name:      "app",
			LastState: k8sContainerState{Terminated: &k8sReason{Reason: "OOMKilled"}},
		}}
		return p
	}

	cases := []struct {
		name string
		pod  k8sPod
		want string
	}{
		{"crashloop", waiting("CrashLoopBackOff"), "CrashLoopBackOff"},
		{"imagepull", waiting("ImagePullBackOff"), "ImagePullBackOff"},
		{"oomkilled", oom(), "OOMKilled"},
		{"healthy-waiting", waiting("ContainerCreating"), ""},
		{"empty", k8sPod{}, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got, _ := crashReason(c.pod); got != c.want {
				t.Errorf("crashReason = %q, want %q", got, c.want)
			}
		})
	}
}
