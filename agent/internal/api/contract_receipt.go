package api

import (
	"context"

	"github.com/jungsoo7496-blip/oceandata/agent/internal/contractapi"
)

// Receipt는 계약에서 생성한 요청 경로와 기존 인증·재시도를 사용한다.
// ByteSize의 필드 누락까지 검증하기 위해 업무용 응답 타입을 유지한다.
func (c *Client) Receipt(ctx context.Context, assetID string) (Receipt, error) {
	req, err := contractapi.NewGetAssetReceiptRequest("/", assetID)
	if err != nil {
		return Receipt{}, err
	}
	var receipt Receipt
	_, err = c.Do(ctx, req.Method, req.URL.RequestURI(), nil, nil, &receipt)
	return receipt, err
}
