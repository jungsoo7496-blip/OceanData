"""report version period (보고서 버전마다 데이터 기간, 계약 점검 S34, ADR-0080)

Revision ID: 0025
Revises: 0024
"""
from alembic import op
import sqlalchemy as sa

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 전에는 reports 행 하나의 기간을 모든 버전이 같이 읽어, 회차 보고서의 새 버전이 앞 버전(승인·제출본 포함)의
    # 기간까지 바꿔 보였다. 버전마다 기간을 둔다. 이미 있는 버전은 지금 reports 행의 기간으로 채운다 — 앞서 바뀐
    # 기간은 DB에 남아 있지 않다(승인본의 원래 기간은 EVIDENCE 존의 승인 스냅숏에 있다)
    op.add_column('report_versions', sa.Column('period_from', sa.DateTime(timezone=True), nullable=True))
    op.add_column('report_versions', sa.Column('period_to', sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE report_versions v SET period_from = r.period_from, period_to = r.period_to "
               "FROM reports r WHERE r.report_id = v.report_id")


def downgrade() -> None:
    # 버전별 기간만 버린다. reports 행의 기간(최신 버전의 기간)은 그대로라 되돌린 코드가 읽을 수 있다
    op.drop_column('report_versions', 'period_to')
    op.drop_column('report_versions', 'period_from')
