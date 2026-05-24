// Unit tests for Phase 12 PodDebuggerApprovalPolicy evaluation.
package main

import (
	"encoding/json"
	"testing"
)

// --- decoding --------------------------------------------------------------

func TestApprovalPolicyDecode(t *testing.T) {
	raw := `{
		"kind": "PodDebuggerApprovalPolicy",
		"metadata": {"name": "prod-allow-restart"},
		"spec": {
			"scope": {
				"namespaceSelector": {"matchLabels": {"tier": "prod"}}
			},
			"rules": [
				{"kind": "remediation", "action": "restart", "decision": "allow"},
				{"kind": "remediation", "action": "rollback", "decision": "deny"}
			]
		}
	}`
	var p ApprovalPolicy
	if err := json.Unmarshal([]byte(raw), &p); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if p.Metadata.Name != "prod-allow-restart" {
		t.Errorf("name = %q", p.Metadata.Name)
	}
	if got := p.Spec.Scope.NamespaceSelector.MatchLabels["tier"]; got != "prod" {
		t.Errorf("namespaceSelector tier label = %q, want prod", got)
	}
	if len(p.Spec.Rules) != 2 {
		t.Fatalf("rules = %d, want 2", len(p.Spec.Rules))
	}
}

func TestApprovalPolicyListDecode(t *testing.T) {
	raw := `{
		"kind": "List",
		"items": [
			{"kind": "PodDebuggerApprovalPolicy", "metadata": {"name": "p1"},
			 "spec": {"rules": []}},
			{"kind": "PodDebuggerApprovalPolicy", "metadata": {"name": "p2"},
			 "spec": {"rules": []}}
		]
	}`
	var list ApprovalPolicy
	if err := json.Unmarshal([]byte(raw), &list); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if list.Kind != "List" || len(list.Items) != 2 {
		t.Errorf("list shape wrong: kind=%q items=%d", list.Kind, len(list.Items))
	}
}

// --- scope matching --------------------------------------------------------

func TestScopeEmptySelectorMatchesEverything(t *testing.T) {
	scope := ApprovalPolicyScope{}
	if !scopeMatches(scope, nil) {
		t.Error("empty selector should match nil labels")
	}
	if !scopeMatches(scope, map[string]string{"tier": "prod"}) {
		t.Error("empty selector should match labeled namespace too")
	}
}

func TestScopeMatchLabelsRequireAll(t *testing.T) {
	scope := ApprovalPolicyScope{NamespaceSelector: LabelSelector{
		MatchLabels: map[string]string{"tier": "prod", "team": "payments"},
	}}
	// missing one label = no match
	if scopeMatches(scope, map[string]string{"tier": "prod"}) {
		t.Error("partial label match should not satisfy multi-label selector")
	}
	// all labels present and equal = match
	if !scopeMatches(scope, map[string]string{
		"tier": "prod", "team": "payments", "extra": "ignored"}) {
		t.Error("all required labels present should match")
	}
	// wrong value = no match
	if scopeMatches(scope, map[string]string{"tier": "staging", "team": "payments"}) {
		t.Error("mismatched label value should not satisfy selector")
	}
}

// --- decision resolution ---------------------------------------------------

func TestEvaluatePoliciesAllowSimple(t *testing.T) {
	policies := []ApprovalPolicy{{
		Spec: ApprovalPolicySpec{Rules: []ApprovalPolicyRule{
			{Kind: "remediation", Action: "restart", Decision: "allow"},
		}},
	}}
	got := EvaluatePolicies(policies, nil, "restart")
	if got != PolicyAllow {
		t.Errorf("got %q, want allow", got)
	}
}

func TestEvaluatePoliciesNoMatchReturnsEmpty(t *testing.T) {
	policies := []ApprovalPolicy{{
		Spec: ApprovalPolicySpec{Rules: []ApprovalPolicyRule{
			{Kind: "remediation", Action: "scale", Decision: "allow"},
		}},
	}}
	if got := EvaluatePolicies(policies, nil, "restart"); got != "" {
		t.Errorf("got %q, want empty", got)
	}
}

func TestEvaluatePoliciesDenyBeatsAllow(t *testing.T) {
	policies := []ApprovalPolicy{
		{Spec: ApprovalPolicySpec{Rules: []ApprovalPolicyRule{
			{Kind: "remediation", Action: "restart", Decision: "allow"},
		}}},
		{Spec: ApprovalPolicySpec{Rules: []ApprovalPolicyRule{
			{Kind: "remediation", Action: "restart", Decision: "deny"},
		}}},
	}
	if got := EvaluatePolicies(policies, nil, "restart"); got != PolicyDeny {
		t.Errorf("got %q, want deny", got)
	}
}

func TestEvaluatePoliciesRequiresApprovalBeatsAllow(t *testing.T) {
	policies := []ApprovalPolicy{
		{Spec: ApprovalPolicySpec{Rules: []ApprovalPolicyRule{
			{Kind: "remediation", Action: "set-resources", Decision: "allow"},
		}}},
		{Spec: ApprovalPolicySpec{Rules: []ApprovalPolicyRule{
			{Kind: "remediation", Action: "set-resources", Decision: "requires-approval"},
		}}},
	}
	if got := EvaluatePolicies(policies, nil, "set-resources"); got != PolicyRequiresApproval {
		t.Errorf("got %q, want requires-approval", got)
	}
}

func TestEvaluatePoliciesScopeFiltersByLabels(t *testing.T) {
	policies := []ApprovalPolicy{{
		Spec: ApprovalPolicySpec{
			Scope: ApprovalPolicyScope{
				NamespaceSelector: LabelSelector{MatchLabels: map[string]string{"tier": "prod"}},
			},
			Rules: []ApprovalPolicyRule{
				{Kind: "remediation", Action: "restart", Decision: "deny"},
			},
		},
	}}
	// staging namespace -> policy doesn't apply -> empty decision
	if got := EvaluatePolicies(policies, map[string]string{"tier": "staging"}, "restart"); got != "" {
		t.Errorf("staging: got %q, want empty (policy out of scope)", got)
	}
	// prod namespace -> policy applies -> deny
	if got := EvaluatePolicies(policies, map[string]string{"tier": "prod"}, "restart"); got != PolicyDeny {
		t.Errorf("prod: got %q, want deny", got)
	}
}

func TestEvaluatePoliciesEmptySliceIsEmptyDecision(t *testing.T) {
	if got := EvaluatePolicies(nil, nil, "restart"); got != "" {
		t.Errorf("got %q, want empty", got)
	}
}

func TestRuleAppliesRejectsNonRemediationKind(t *testing.T) {
	rule := ApprovalPolicyRule{Kind: "probe", Action: "restart", Decision: "allow"}
	if ruleApplies(rule, "restart") {
		t.Error("policy with kind=probe shouldn't match remediation actions in v1")
	}
}

func TestRuleAppliesActionWildcardWhenEmpty(t *testing.T) {
	// rule.Action == "" should match any action (catch-all). Useful for
	// "deny everything in this namespace" policies.
	rule := ApprovalPolicyRule{Kind: "remediation", Decision: "deny"}
	if !ruleApplies(rule, "restart") {
		t.Error("empty action should match every remediation action")
	}
	if !ruleApplies(rule, "scale") {
		t.Error("empty action should match every remediation action")
	}
}

// --- expiry ----------------------------------------------------------------

func TestRuleExpiryPast(t *testing.T) {
	rule := ApprovalPolicyRule{Kind: "remediation", Action: "restart",
		Decision: "allow", Expires: "1999-01-01"}
	if ruleApplies(rule, "restart") {
		t.Error("expired rule shouldn't apply")
	}
}

func TestRuleExpiryFuture(t *testing.T) {
	rule := ApprovalPolicyRule{Kind: "remediation", Action: "restart",
		Decision: "allow", Expires: "2999-12-31"}
	if !ruleApplies(rule, "restart") {
		t.Error("future-expiry rule should still apply")
	}
}

func TestRuleExpiryMalformedTreatedAsExpired(t *testing.T) {
	rule := ApprovalPolicyRule{Kind: "remediation", Action: "restart",
		Decision: "allow", Expires: "not-a-date"}
	if ruleApplies(rule, "restart") {
		t.Error("malformed expiry should be treated as expired so the admin notices")
	}
}

// --- end-to-end CR-level precedence ---------------------------------------

func TestActionInListHelper(t *testing.T) {
	if !actionInList("restart", []string{"scale", "restart"}) {
		t.Error("present action should be found")
	}
	if actionInList("rollback", []string{"scale", "restart"}) {
		t.Error("absent action should not be found")
	}
	if actionInList("restart", nil) {
		t.Error("nil list shouldn't contain anything")
	}
}
