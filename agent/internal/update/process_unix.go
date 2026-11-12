//go:build !windows

package update

import "os/exec"

func configureBackgroundProcess(_ *exec.Cmd) {}
