"""policy drafts (09번 요구사항 3.2·3.3, ADR-0037)

Revision ID: 0023
Revises: 0022
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0023'
down_revision = '0022'  # 병합하며 0022(Hot/Warm 계층) 뒤로 다시 이음
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 장비 정책 초안: 운영자가 적용하기 전까지 장비 기록·정책을 바꾸지 않는다. 다른 표는 건드리지 않는다
    op.create_table('policy_drafts',
    sa.Column('policy_draft_id', sa.String(length=30), nullable=False),
    sa.Column('organization_id', sa.String(length=30), nullable=False),
    sa.Column('device_id', sa.String(length=30), nullable=False),
    sa.Column('collector_device_id', sa.String(length=30), nullable=False),
    sa.Column('integration_card_id', sa.String(length=30), nullable=True),
    sa.Column('integration_card_version', sa.Integer(), nullable=True),
    sa.Column('fields', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=12), server_default='DRAFT', nullable=False),
    sa.Column('revision', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_by', sa.String(length=64), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_reason', sa.String(length=500), nullable=True),
    sa.Column('applied', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['collector_device_id'], ['devices.device_id'], name=op.f('fk_policy_drafts_collector_device_id_devices')),
    sa.ForeignKeyConstraint(['device_id'], ['devices.device_id'], name=op.f('fk_policy_drafts_device_id_devices')),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.organization_id'], name=op.f('fk_policy_drafts_organization_id_organizations')),
    sa.PrimaryKeyConstraint('policy_draft_id', name=op.f('pk_policy_drafts'))
    )
    op.create_index(op.f('ix_policy_drafts_device_id'), 'policy_drafts', ['device_id'], unique=False)
    op.create_index(op.f('ix_policy_drafts_organization_id'), 'policy_drafts', ['organization_id'], unique=False)
    # 장비마다 검토 중(DRAFT) 초안은 하나 (동시에 만들어도 하나만 남는다)
    op.create_index('uq_policy_drafts_open_device', 'policy_drafts', ['device_id'], unique=True, postgresql_where=sa.text("status = 'DRAFT'"))


def downgrade() -> None:
    # 초안 기록만 지운다. 이미 적용된 초안이 쓴 장비 정책 버전(device_policies)과 장비 기록은 그대로 남는다
    op.drop_index('uq_policy_drafts_open_device', table_name='policy_drafts', postgresql_where=sa.text("status = 'DRAFT'"))
    op.drop_index(op.f('ix_policy_drafts_organization_id'), table_name='policy_drafts')
    op.drop_index(op.f('ix_policy_drafts_device_id'), table_name='policy_drafts')
    op.drop_table('policy_drafts')
