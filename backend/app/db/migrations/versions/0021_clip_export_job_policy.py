"""clip export job policy

Revision ID: 0021
Revises: 0020
"""
from alembic import op

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 구간 클립 내보내기 (09번 요구사항 7.2): 사용자가 기다리는 작업이라 썸네일 다음 우선순위,
    # 긴 구간 재인코딩이 오래 걸릴 수 있어 임대 2시간. 일시 오류는 3번까지 (인코딩 실패는 바로 FAILED)
    op.execute("""INSERT INTO job_policies (job_type, priority, resource_class, gpu_fallback, max_attempts, lease_s)
                  VALUES ('clip_export', 70, 'CPU', 'CPU', 3, 7200) ON CONFLICT (job_type) DO NOTHING""")


def downgrade() -> None:
    # 되돌린 코드에는 처리기가 없어 대기 중인 클립 작업이 영원히 남으므로 지운다 (만든 클립 자산은 그대로)
    op.execute("DELETE FROM jobs WHERE job_type = 'clip_export' AND status IN ('QUEUED', 'RUNNING')")
    op.execute("DELETE FROM job_policies WHERE job_type = 'clip_export'")
