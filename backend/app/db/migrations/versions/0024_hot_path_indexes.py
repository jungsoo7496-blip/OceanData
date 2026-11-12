"""hot path indexes (작업 큐 꺼내기·주기 감시·목록 조회, ADR-0042)

Revision ID: 0024
Revises: 0023
"""
from alembic import op
import sqlalchemy as sa

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 인덱스만 바꾼다 (표·API·결과 순서는 그대로). 실측 근거와 전후 시간은 ADR-0042.
    # 작업 큐 꺼내기(pg_queue.claim, 워커마다 쉬지 않고 실행): QUEUED만, ORDER BY priority DESC, run_after, created_at 그대로.
    # ix_jobs_claim(status, priority, run_after)은 run_after 방향이 반대라 대기 작업이 쌓이면 최상위 우선순위 묶음 전체를
    # 매번 읽어 정렬했다. ix_jobs_claim은 RUNNING·FAILED 조회(임대 회수·실패 원인)에 계속 쓴다
    op.create_index('ix_jobs_claim_queued', 'jobs', [sa.text('priority DESC'), 'run_after', 'created_at'], unique=False,
                    postgresql_where=sa.text("status = 'QUEUED'"))
    # 만료 업로드 정리(ingest.sweep_expired, 5분마다): 진행 중(UPLOADING)만 담는다. 전체 expires_at 인덱스는 이미 끝난
    # 업로드를 모두 훑었고 다른 조회는 쓰지 않아 교체한다
    op.create_index('ix_uploads_uploading_expires', 'uploads', ['expires_at'], unique=False,
                    postgresql_where=sa.text("status = 'UPLOADING'"))
    # 감시 규칙 업로드 실패율(1분마다): 최근 window_s 동안 만든 업로드
    op.create_index('ix_uploads_created_at', 'uploads', ['created_at'], unique=False)
    # 수집 상태 API(대시보드가 1분마다)·정기 점검의 최근 24시간 실패 원인: 오류 코드가 있는 업로드만
    op.create_index('ix_uploads_failed_updated', 'uploads', ['updated_at'], unique=False,
                    postgresql_where=sa.text('error_code IS NOT NULL'))
    # 감시 규칙(수집량 급감·격리 급증·용량 예측·회차 무수집, 1분마다)·최근 실패 원인: 최근 들어온 자산만 읽는다
    op.create_index('ix_assets_ingested_at', 'assets', ['ingested_at'], unique=False)
    # 자산 목록 상태 필터(대시보드 처리 대기·격리·실패): 기관·상태 안에서 커서 순서(asset_id)대로 읽어 limit에서 멈춘다.
    # 대부분인 READY_PRIVATE는 빼서 작게 둔다 (그 상태 목록은 기본키 순서로 바로 찬다)
    op.create_index('ix_assets_not_ready', 'assets', ['organization_id', 'ingest_status', 'asset_id'], unique=False,
                    postgresql_where=sa.text("ingest_status <> 'READY_PRIVATE'"))
    # 관측값 시간축 조회(기관 전체·회차 필터, from·to 필수): 월 파티션마다 만들어지고 새 파티션에도 자동으로 붙는다
    op.create_index('ix_observations_org_time', 'observations', ['organization_id', 'observed_at'], unique=False)
    # 기존 업로드 인덱스 삭제에 필요한 읽기 잠금은 마지막까지 미룬다.
    op.drop_index(op.f('ix_uploads_expires_at'), table_name='uploads')


def downgrade() -> None:
    # 0023의 인덱스 구성으로 되돌린다 (자료는 건드리지 않는다)
    op.drop_index('ix_observations_org_time', table_name='observations')
    op.drop_index('ix_assets_not_ready', table_name='assets', postgresql_where=sa.text("ingest_status <> 'READY_PRIVATE'"))
    op.drop_index('ix_assets_ingested_at', table_name='assets')
    op.drop_index('ix_jobs_claim_queued', table_name='jobs', postgresql_where=sa.text("status = 'QUEUED'"))
    op.create_index(op.f('ix_uploads_expires_at'), 'uploads', ['expires_at'], unique=False)
    op.drop_index('ix_uploads_failed_updated', table_name='uploads', postgresql_where=sa.text('error_code IS NOT NULL'))
    op.drop_index('ix_uploads_created_at', table_name='uploads')
    op.drop_index('ix_uploads_uploading_expires', table_name='uploads', postgresql_where=sa.text("status = 'UPLOADING'"))
