// Package timex는 서버·설정이 준 정수 초를 time.Duration으로 넘치지 않게 바꾼다 (ADR-0040).
//
// time.Duration은 int64 나노초라 9,223,372,036초(약 292년)를 넘는 초에 time.Second를 곱하면 음수나 작은 값으로
// 넘친다. 퍼즈 시험에서 아주 큰 하트비트 주기가 음수 대기가 되어 하트비트 루프가 쉬지 않고 돈 것이 그 예다.
// 에이전트는 정수 초를 이 꾸러미로만 time.Duration으로 바꾼다.
package timex

import "time"

// MaxSeconds는 장비 정책 계약(contracts/schemas/device-policy.schema.json)의 초 단위 항목 상한과 같은
// 2147483647초(32비트 정수, 약 68년)다. time.Duration으로 바꿔도, 지금 시각에 더해 UnixNano(2262년까지)로 저장해도
// 넘치지 않는다.
const MaxSeconds = 1<<31 - 1

// Seconds는 n초를 time.Duration으로 바꾼다. 음수는 0으로, MaxSeconds보다 큰 값은 MaxSeconds로 자른다.
func Seconds(n int64) time.Duration {
	return time.Duration(min(max(n, 0), MaxSeconds)) * time.Second
}
