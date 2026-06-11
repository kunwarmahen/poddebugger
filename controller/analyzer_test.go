package main

import (
	"encoding/json"
	"strings"
	"testing"
)

// Regression for a live-cluster finding: the status patch must carry the
// CRD's camelCase field names and tolerate object-valued params (set-env's
// env map), while the same types still decode the CLI's snake_case output.
func TestStatusEncodingIsCamelCaseWithObjectParams(t *testing.T) {
	cliJSON := []byte(`{"action":"set-env",
		"params":{"container":"app","env":{"APP_TOKEN":"x"}},
		"risk":"medium","expected_effect":"starts","validated":true}`)
	var p ProposedRemediation
	if err := json.Unmarshal(cliJSON, &p); err != nil {
		t.Fatalf("decode CLI json: %v", err)
	}
	if p.ExpectedEffect != "starts" {
		t.Fatalf("snake_case decode broken: %+v", p)
	}
	out, err := json.Marshal(pdrStatus{Phase: "x", ProposedRemediation: &p,
		Remediation: &RemediationResult{Action: "set-env", Executed: true,
			Verification: &Verification{Outcome: "unknown", WaitedSeconds: 5},
			SavedTo:      "/tmp/x.json"}})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	s := string(out)
	for _, want := range []string{`"expectedEffect"`, `"savedTo"`,
		`"waitedSeconds"`, `"env":{"APP_TOKEN":"x"}`} {
		if !strings.Contains(s, want) {
			t.Errorf("status payload missing %s: %s", want, s)
		}
	}
	for _, bad := range []string{"expected_effect", "saved_to", "waited_seconds"} {
		if strings.Contains(s, bad) {
			t.Errorf("status payload leaked snake_case %s: %s", bad, s)
		}
	}
}

// Regression for a live-cluster finding: the operator runs the CLI without
// a TTY, where the Phase 11 approval gate denies by default. The CR is the
// human's durable authorization, so the remediate invocation must carry
// --yes — without it every operator remediation was refused.
func TestRemediationArgsCarryYes(t *testing.T) {
	args := remediationArgs("web-1", "prod", "restart", map[string]string{
		"b": "2", "a": "1",
	})
	joined := strings.Join(args, " ")
	for _, want := range []string{"--confirm", "--yes", "--json", "-n prod"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("remediation args missing %q: %v", want, args)
		}
	}
	// params keep stable (sorted) ordering
	if !strings.Contains(joined, "--param a=1 --param b=2") {
		t.Fatalf("params not stably ordered: %v", args)
	}
}
