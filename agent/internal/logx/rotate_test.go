package logx

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRotation(t *testing.T) {
	p := filepath.Join(t.TempDir(), "agent.log")
	r, err := NewRotator(p, 100, 2)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 10; i++ {
		_, _ = r.Write([]byte(strings.Repeat("x", 40) + "\n"))
	}
	_ = r.Close()
	for _, name := range []string{p, p + ".1", p + ".2"} {
		if st, err := os.Stat(name); err != nil || st.Size() > 100 {
			t.Fatalf("%s: %v", name, err)
		}
	}
	if _, err := os.Stat(p + ".3"); err == nil {
		t.Fatal("보관 개수를 넘었다")
	}
}
