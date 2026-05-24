package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
)

// KubernetesWatcher tails `kubectl get pods --watch -o json` and emits a
// CrashEvent when a pod enters CrashLoopBackOff, an image-pull failure, or is
// OOMKilled. Shells out to kubectl/oc — no client-go dependency.
type KubernetesWatcher struct {
	Binary    string // kubectl | oc
	Namespace string // "" => all namespaces
}

type k8sReason struct {
	Reason  string `json:"reason"`
	Message string `json:"message"`
}

type k8sContainerState struct {
	Waiting    *k8sReason `json:"waiting"`
	Terminated *k8sReason `json:"terminated"`
}

type k8sContainerStatus struct {
	Name      string            `json:"name"`
	State     k8sContainerState `json:"state"`
	LastState k8sContainerState `json:"lastState"`
}

type k8sPod struct {
	Kind     string `json:"kind"`
	Metadata struct {
		Name      string `json:"name"`
		Namespace string `json:"namespace"`
	} `json:"metadata"`
	Status struct {
		ContainerStatuses []k8sContainerStatus `json:"containerStatuses"`
	} `json:"status"`
	Items []k8sPod `json:"items"` // populated when Kind == "List"
}

// crashLoopReasons are container waiting-state reasons treated as crashes.
var crashLoopReasons = map[string]bool{
	"CrashLoopBackOff":           true,
	"ImagePullBackOff":           true,
	"ErrImagePull":               true,
	"CreateContainerError":       true,
	"CreateContainerConfigError": true,
	"RunContainerError":          true,
}

// crashReason returns (reason, containerName) if the pod looks crashed.
func crashReason(p k8sPod) (string, string) {
	for _, cs := range p.Status.ContainerStatuses {
		if cs.State.Waiting != nil && crashLoopReasons[cs.State.Waiting.Reason] {
			return cs.State.Waiting.Reason, cs.Name
		}
		if cs.State.Terminated != nil && cs.State.Terminated.Reason == "OOMKilled" {
			return "OOMKilled", cs.Name
		}
		if cs.LastState.Terminated != nil && cs.LastState.Terminated.Reason == "OOMKilled" {
			return "OOMKilled", cs.Name
		}
	}
	return "", ""
}

func (w KubernetesWatcher) Watch(ctx context.Context, out chan<- CrashEvent) error {
	args := []string{"get", "pods", "--watch", "-o", "json"}
	if w.Namespace != "" {
		args = append(args, "-n", w.Namespace)
	} else {
		args = append(args, "--all-namespaces")
	}
	cmd := exec.CommandContext(ctx, w.Binary, args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("starting %s watch: %w", w.Binary, err)
	}

	dec := json.NewDecoder(stdout)
	for {
		var obj k8sPod
		if err := dec.Decode(&obj); err != nil {
			_ = cmd.Wait()
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return err
		}
		pods := obj.Items // Kind == "List"
		if obj.Kind == "Pod" || len(pods) == 0 {
			pods = []k8sPod{obj}
		}
		for _, p := range pods {
			reason, container := crashReason(p)
			if reason == "" {
				continue
			}
			select {
			case out <- CrashEvent{
				Platform:  "kubernetes",
				Target:    p.Metadata.Name,
				Namespace: p.Metadata.Namespace,
				Reason:    reason,
				Detail:    "container " + container,
			}:
			case <-ctx.Done():
				_ = cmd.Wait()
				return ctx.Err()
			}
		}
	}
}
