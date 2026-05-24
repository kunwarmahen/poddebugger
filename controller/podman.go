package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
)

// PodmanWatcher tails `podman events` and emits a CrashEvent for every
// container that dies with a non-zero exit code.
type PodmanWatcher struct {
	Binary string // "podman"
}

// podmanEvent is the subset of `podman events --format json` we use.
type podmanEvent struct {
	Name              string `json:"Name"`
	Status            string `json:"Status"`
	Type              string `json:"Type"`
	ContainerExitCode int    `json:"ContainerExitCode"`
}

func (w PodmanWatcher) Watch(ctx context.Context, out chan<- CrashEvent) error {
	cmd := exec.CommandContext(ctx, w.Binary, "events",
		"--filter", "type=container", "--filter", "event=died", "--format", "json")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("starting podman events: %w", err)
	}

	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		var ev podmanEvent
		if err := json.Unmarshal(scanner.Bytes(), &ev); err != nil {
			continue
		}
		if ev.Type != "container" || ev.Status != "died" {
			continue
		}
		if ev.ContainerExitCode == 0 {
			continue // a clean stop, not a crash
		}
		select {
		case out <- CrashEvent{
			Platform: "podman",
			Target:   ev.Name,
			Reason:   "died",
			Detail:   fmt.Sprintf("exit code %d", ev.ContainerExitCode),
		}:
		case <-ctx.Done():
			_ = cmd.Wait()
			return ctx.Err()
		}
	}
	_ = cmd.Wait()
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return scanner.Err()
}
