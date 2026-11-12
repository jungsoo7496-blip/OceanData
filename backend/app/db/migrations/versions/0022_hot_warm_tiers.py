"""hot warm tiers

Revision ID: 0022
Revises: 0021
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 08번 요구사항 9 (ADR-0036): 자산 객체의 계층·마지막 조회, 이동 규칙 표, 이동 기록, 계층별 사용량 스냅숏
    op.add_column('assets', sa.Column('storage_tier', sa.String(length=10), server_default='HOT', nullable=False))
    op.add_column('assets', sa.Column('last_accessed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_assets_tier_age', 'assets', ['storage_tier', 'storage_zone', 'ingested_at'], unique=False,
                    postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_table('tier_policies',
    sa.Column('policy_id', sa.String(length=64), nullable=False),
    sa.Column('zone', sa.String(length=20), nullable=False),
    sa.Column('variant', sa.String(length=20), nullable=True),
    sa.Column('min_age_days', sa.Integer(), nullable=False),
    sa.Column('min_idle_days', sa.Integer(), nullable=True),
    sa.Column('enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('policy_id', name=op.f('pk_tier_policies'))
    )
    op.create_table('tier_moves',
    sa.Column('move_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('asset_id', sa.String(length=30), nullable=False),
    sa.Column('organization_id', sa.String(length=30), nullable=False),
    sa.Column('policy_id', sa.String(length=64), nullable=True),
    sa.Column('from_tier', sa.String(length=10), nullable=False),
    sa.Column('to_tier', sa.String(length=10), nullable=False),
    sa.Column('zone', sa.String(length=20), nullable=False),
    sa.Column('object_key', sa.String(length=400), nullable=False),
    sa.Column('byte_size', sa.BigInteger(), nullable=False),
    sa.Column('checksum_sha256', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('error_code', sa.String(length=60), nullable=True),
    sa.Column('legal_hold', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('switched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delete_after', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.asset_id'], name=op.f('fk_tier_moves_asset_id_assets')),
    sa.PrimaryKeyConstraint('move_id', name=op.f('pk_tier_moves'))
    )
    op.create_index(op.f('ix_tier_moves_asset_id'), 'tier_moves', ['asset_id'], unique=False)
    op.create_index('ix_tier_moves_status_due', 'tier_moves', ['status', 'delete_after'], unique=False)
    # 자산마다 열린 이동은 하나: 되돌리는 이동이 유예 중인 원래 객체 자리에 쓰는 일을 막는다
    op.create_index('uq_tier_moves_open', 'tier_moves', ['asset_id'], unique=True,
                    postgresql_where=sa.text("status IN ('COPYING', 'SWITCHED')"))
    op.add_column('usage_snapshots', sa.Column('by_tier', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    # 규칙 초안: 들어온 지 180일 지나고 90일 동안 열지 않은 원본(RAW)만 WARM으로. 값은 정책 — 운영기관 확정 필요.
    # 처음에는 꺼 둔다 (TIERING_ENABLED와 규칙 enabled를 모두 켜야 옮긴다)
    op.execute("""INSERT INTO tier_policies (policy_id, zone, variant, min_age_days, min_idle_days, enabled, updated_at)
                  VALUES ('raw_original_to_warm', 'RAW', 'ORIGINAL', 180, 90, false, now())
                  ON CONFLICT (policy_id) DO NOTHING""")
    # 계층 이동: 배경 작업이라 낮은 우선순위, 큰 원본 복사·재검증이 오래 걸릴 수 있어 임대 6시간, 일시 오류는 3번까지
    # (사본·원본 해시 불일치는 바로 FAILED)
    op.execute("""INSERT INTO job_policies (job_type, priority, resource_class, gpu_fallback, max_attempts, lease_s)
                  VALUES ('tier_move', 0, 'CPU', 'CPU', 3, 21600) ON CONFLICT (job_type) DO NOTHING""")


def downgrade() -> None:
    # 되돌린 코드는 계층을 모른다: WARM에 있는 자산은 읽을 수 없게 되고, 진행 중 이동의 사본·유예 중 원래 객체는
    # 고아가 된다. 그런 상태면 멈추고 먼저 `python -m app.cli tiers --recall`로 HOT으로 되돌리게 한다
    conn = op.get_bind()
    warm = conn.execute(sa.text("SELECT count(*) FROM assets WHERE storage_tier <> 'HOT' AND deleted_at IS NULL")).scalar()
    open_moves = conn.execute(sa.text("SELECT count(*) FROM tier_moves WHERE status IN ('COPYING', 'SWITCHED')")).scalar()
    if warm or open_moves:
        raise RuntimeError(f"WARM 자산 {warm}건·진행 중 계층 이동 {open_moves}건이 있습니다. "
                           "python -m app.cli tiers --recall로 HOT으로 되돌리고 유예 삭제가 끝난 뒤 다시 하세요")
    op.execute("DELETE FROM jobs WHERE job_type = 'tier_move' AND status IN ('QUEUED', 'RUNNING')")
    op.execute("DELETE FROM job_policies WHERE job_type = 'tier_move'")
    op.drop_column('usage_snapshots', 'by_tier')
    op.drop_index('uq_tier_moves_open', table_name='tier_moves', postgresql_where=sa.text("status IN ('COPYING', 'SWITCHED')"))
    op.drop_index('ix_tier_moves_status_due', table_name='tier_moves')
    op.drop_index(op.f('ix_tier_moves_asset_id'), table_name='tier_moves')
    op.drop_table('tier_moves')
    op.drop_table('tier_policies')
    op.drop_index('ix_assets_tier_age', table_name='assets', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_column('assets', 'last_accessed_at')
    op.drop_column('assets', 'storage_tier')
