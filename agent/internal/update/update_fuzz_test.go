package update

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"strings"
	"testing"
)

// 퍼즈 시험: 업데이트 바이너리와 서명 파일은 내려받았거나 손으로 옮긴, 변조됐을 수 있는 바이트다 (06번 요구사항 12.1).
// 실행: go test -run='^$' -fuzz='^FuzzVerify$' -fuzztime=90s -parallel=2 ./internal/update
func FuzzVerify(f *testing.F) {
	// 시험 전용 합성 키. 말뭉치의 서명이 다음 실행에도 유효하도록 고정 문자열에서 만든다 (실제 배포 키가 아니다)
	keySeed := sha256.Sum256([]byte("oceandata agent fuzz synthetic update key"))
	priv := ed25519.NewKeyFromSeed(keySeed[:])
	key := base64.StdEncoding.EncodeToString(priv.Public().(ed25519.PublicKey))
	sigFile := func(data []byte) []byte {
		return []byte(base64.StdEncoding.EncodeToString(ed25519.Sign(priv, data)) + "\n")
	}
	f.Add([]byte("v2"), sigFile([]byte("v2")), uint16(0))          // TestVerifyApplyRollback의 예
	f.Add([]byte("v2-tampered"), sigFile([]byte("v2")), uint16(3)) // 변조된 바이너리
	f.Add([]byte("v2"), append([]byte(" \r\n"), sigFile([]byte("v2"))...), uint16(17))
	f.Add([]byte{}, sigFile(nil), uint16(0))
	f.Add([]byte("\x7fELF\x02\x01\x01"), []byte("not base64!"), uint16(9))
	f.Add([]byte("v2"), []byte(base64.StdEncoding.EncodeToString(make([]byte, ed25519.SignatureSize))), uint16(0))
	f.Fuzz(func(t *testing.T, data, sig []byte, pos uint16) {
		// 1) 서명 파일은 1024바이트 한도로 읽은 뒤 검증한다 (Fetch·Local과 같은 순서).
		//    통과했다면 그 내용은 이 키로 data에 서명한 값이어야 한다. Ed25519 서명은 결정적이고
		//    crypto/ed25519는 비정규 S를 거부하므로 개인키 없이 다른 유효 서명을 만들 수 없다
		read, err := readLimited(bytes.NewReader(sig), maxSignatureBytes)
		switch {
		case err != nil:
			if len(sig) > 0 && int64(len(sig)) <= maxSignatureBytes {
				t.Fatalf("한도 안의 서명 파일을 거부했다: %v", err)
			}
		case !bytes.Equal(read, sig):
			t.Fatalf("서명 파일을 다르게 읽었다: %q", read)
		case verify(data, read, key) == nil:
			got, decErr := base64.StdEncoding.DecodeString(strings.TrimSpace(string(read)))
			if decErr != nil || !bytes.Equal(got, ed25519.Sign(priv, data)) {
				t.Fatalf("이 키로 서명하지 않은 서명 파일 %q를 통과시켰다", read)
			}
		}
		// 2) 정식 서명은 통과하고, 바이너리를 한 비트라도 바꾸거나 늘리거나 줄이면 거부한다
		good := sigFile(data)
		if err := verify(data, good, key); err != nil {
			t.Fatalf("정식 서명을 거부했다: %v", err)
		}
		tampered := [][]byte{append(bytes.Clone(data), byte(pos))}
		if len(data) > 0 {
			flipped := bytes.Clone(data)
			flipped[int(pos>>3)%len(data)] ^= 1 << (pos & 7)
			tampered = append(tampered, flipped, data[:len(data)-1])
		}
		for _, b := range tampered {
			if verify(b, good, key) == nil {
				t.Fatalf("변조된 바이너리 %q를 통과시켰다 (서명한 것은 %q)", b, data)
			}
		}
		// 3) 서명을 한 비트 바꿔도 거부한다
		raw := ed25519.Sign(priv, data)
		raw[int(pos>>3)%len(raw)] ^= 1 << (pos & 7)
		if verify(data, []byte(base64.StdEncoding.EncodeToString(raw)), key) == nil {
			t.Fatal("변조된 서명을 통과시켰다")
		}
	})
}
