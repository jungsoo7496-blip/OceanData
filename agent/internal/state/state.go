// Package state는 Local_State(SQLite, 순수 Go 드라이버)다. 업로드 진행·영수증·대기열을 기록한다.
package state

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	_ "modernc.org/sqlite" // BSD-3 순수 Go SQLite
)

// 파일 상태
const (
	Queued    = "QUEUED"    // 쓰기 완료 판정을 통과해 전송 대기
	Uploading = "UPLOADING" // 서버 업로드 세션이 열려 있음 (재시작 시 남은 파트만)
	Done      = "DONE"      // 바이너리는 검증된 실영수증 보관 완료. C0만 메타데이터 등록 확인
	Excluded  = "EXCLUDED"  // 정책상 보내지 않음 (06번 요구사항 2.4)
	Failed    = "FAILED"    // 재시도해도 안 되는 거부 (형식 오류 등)
)

type File struct {
	ID             int64
	RelPath        string
	RuleID         string
	SourceDeviceID string
	SessionLabel   string
	Size           int64
	MtimeNs        int64
	SHA256         string
	Status         string
	UploadID       string
	AssetID        string
	Priority       int
	Attempts       int
	LastError      string
	DiscoveredAt   time.Time
	NextAttemptAt  time.Time
}

type Store struct {
	db *sql.DB
	// Now는 재시도 시각 계산에 쓴다 (시험에서 바꾼다).
	Now func() time.Time
}

// ErrStaleFile은 스캔·전송 사이 파일 버전이 바뀌었거나 행이 사라졌다는 뜻이다.
// 이전 전송의 영수증으로 새 파일 버전을 완료 처리하지 않는다.
var ErrStaleFile = errors.New("로컬 파일 상태가 바뀌어 이전 전송 결과를 적용하지 않습니다")

func oneFile(result sql.Result) error {
	n, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return ErrStaleFile
	}
	return nil
}

func Open(dir string) (*Store, error) {
	dbPath := filepath.Join(dir, "state.db")
	if f, err := os.OpenFile(dbPath, os.O_CREATE|os.O_RDWR, 0o600); err == nil { // 파일 경로 등 현장 정보가 있다
		_ = f.Close()
	}
	db, err := sql.Open("sqlite", "file:"+dbPath+"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)&_pragma=synchronous(NORMAL)")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	s := &Store{db: db, Now: time.Now}
	return s, s.migrate()
}

func (s *Store) Close() error { return s.db.Close() }

func (s *Store) migrate() error {
	_, err := s.db.Exec(`
	CREATE TABLE IF NOT EXISTS files (
	  id INTEGER PRIMARY KEY AUTOINCREMENT,
	  rel_path TEXT NOT NULL UNIQUE,
	  rule_id TEXT NOT NULL, source_device_id TEXT NOT NULL, session_label TEXT NOT NULL DEFAULT '',
	  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, sha256 TEXT NOT NULL DEFAULT '',
	  status TEXT NOT NULL, upload_id TEXT NOT NULL DEFAULT '', asset_id TEXT NOT NULL DEFAULT '',
	  priority INTEGER NOT NULL DEFAULT 2, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
	  discovered_at INTEGER NOT NULL, next_attempt_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL
	);
	CREATE INDEX IF NOT EXISTS ix_files_queue ON files(status, priority, discovered_at);
	CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
	-- Sensor_Plugin 관측값 대기열 (07번 요구사항 6.2: 오프라인이면 쌓아 두고 시간순으로 보낸다)
	CREATE TABLE IF NOT EXISTS observations_queue (
	  id INTEGER PRIMARY KEY AUTOINCREMENT, source_device_id TEXT NOT NULL, item TEXT NOT NULL, created_at INTEGER NOT NULL
	);
	CREATE INDEX IF NOT EXISTS ix_obsq_source ON observations_queue(source_device_id, id);
	`)
	return err
}

func now() int64 { return time.Now().UnixNano() }

const cols = `id, rel_path, rule_id, source_device_id, session_label, size, mtime_ns, sha256, status, upload_id,
	asset_id, priority, attempts, last_error, discovered_at, next_attempt_at`

func scan(r interface{ Scan(...any) error }) (*File, error) {
	var f File
	var disc, next int64
	if err := r.Scan(&f.ID, &f.RelPath, &f.RuleID, &f.SourceDeviceID, &f.SessionLabel, &f.Size, &f.MtimeNs,
		&f.SHA256, &f.Status, &f.UploadID, &f.AssetID, &f.Priority, &f.Attempts, &f.LastError, &disc, &next); err != nil {
		return nil, err
	}
	f.DiscoveredAt, f.NextAttemptAt = time.Unix(0, disc), time.Unix(0, next)
	return &f, nil
}

// Get은 상대 경로로 기록을 찾는다. 없으면 nil.
func (s *Store) Get(ctx context.Context, rel string) (*File, error) {
	f, err := scan(s.db.QueryRowContext(ctx, `SELECT `+cols+` FROM files WHERE rel_path = ?`, rel))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return f, err
}

// Enqueue는 쓰기 완료된 파일을 대기열에 넣는다. 같은 경로가 이미 끝났고 크기·수정시각이 같으면 무시한다.
// 끝난 파일의 내용이 바뀌었으면 새 파일로 다시 넣는다 (서버가 sha256으로 중복을 판단한다).
func (s *Store) Enqueue(ctx context.Context, f File) (bool, error) {
	cur, err := s.Get(ctx, f.RelPath)
	if err != nil {
		return false, err
	}
	if cur != nil {
		if cur.Size == f.Size && cur.MtimeNs == f.MtimeNs {
			return false, nil
		}
		if cur.Status == Uploading {
			return false, nil // 전송 중에 바뀐 파일은 서버 해시 검증이 걸러낸다
		}
		tx, err := s.db.BeginTx(ctx, nil)
		if err != nil {
			return false, err
		}
		defer func() { _ = tx.Rollback() }() // Commit 뒤에는 ErrTxDone만 돌려준다
		result, err := tx.ExecContext(ctx, `UPDATE files SET size=?, mtime_ns=?, sha256='', status=?, upload_id='',
			asset_id='', attempts=0, last_error='', rule_id=?, source_device_id=?, session_label=?, priority=?,
			next_attempt_at=0, updated_at=? WHERE id=? AND status!=? AND size=? AND mtime_ns=?`,
			f.Size, f.MtimeNs, Queued, f.RuleID, f.SourceDeviceID, f.SessionLabel, f.Priority, now(), cur.ID,
			Uploading, cur.Size, cur.MtimeNs)
		if err != nil {
			return false, err
		}
		if err := oneFile(result); err != nil {
			if errors.Is(err, ErrStaleFile) {
				return false, nil // Get 이후 다른 스캔·전송이 먼저 상태를 바꿨다.
			}
			return false, err
		}
		if _, err = tx.ExecContext(ctx, `DELETE FROM kv WHERE key IN (?,?)`,
			fmt.Sprintf("receipt_pending:%d", cur.ID), fmt.Sprintf("receipt:%d", cur.ID)); err != nil {
			return false, err
		}
		err = tx.Commit()
		return err == nil, err
	}
	_, err = s.db.ExecContext(ctx, `INSERT INTO files (rel_path, rule_id, source_device_id, session_label, size,
		mtime_ns, status, priority, discovered_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)`, f.RelPath, f.RuleID,
		f.SourceDeviceID, f.SessionLabel, f.Size, f.MtimeNs, Queued, f.Priority, now(), now())
	return err == nil, err
}

// Next는 보낼 차례인 파일들을 돌려준다: 진행 중 업로드 먼저, 그다음 순위 → 오래된 순 (06번 요구사항 7.1).
func (s *Store) Next(ctx context.Context, limit int) ([]*File, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT `+cols+` FROM files WHERE status IN (?, ?) AND next_attempt_at <= ?
		ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END, priority, discovered_at LIMIT ?`,
		Uploading, Queued, s.Now().UnixNano(), Uploading, limit)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }() // 반복 오류는 rows.Err()로 확인한다
	var out []*File
	for rows.Next() {
		f, err := scan(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, f)
	}
	return out, rows.Err()
}

func (s *Store) SetHash(ctx context.Context, id int64, sha string) error {
	_, err := s.db.ExecContext(ctx, `UPDATE files SET sha256=?, updated_at=? WHERE id=?`, sha, now(), id)
	return err
}

func (s *Store) MarkUploading(ctx context.Context, id int64, uploadID, assetID string) error {
	_, err := s.db.ExecContext(ctx, `UPDATE files SET status=?, upload_id=?, asset_id=?, updated_at=? WHERE id=?`,
		Uploading, uploadID, assetID, now(), id)
	return err
}

func (s *Store) MarkDone(ctx context.Context, id int64, assetID string) error {
	_, err := s.db.ExecContext(ctx, `UPDATE files SET status=?, asset_id=?, last_error='', updated_at=? WHERE id=?`,
		Done, assetID, now(), id)
	if err == nil {
		err = s.SetKV(ctx, "last_success_at", time.Now().UTC().Format(time.RFC3339))
	}
	return err
}

// AwaitReceipt는 완료 응답 뒤의 영수증 대기를 남긴다. 전송 세션·원본은 그대로 유지한다.
func (s *Store) AwaitReceipt(ctx context.Context, f *File, assetID string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }() // Commit 뒤에는 ErrTxDone만 돌려준다
	result, err := tx.ExecContext(ctx, `UPDATE files SET status=?, asset_id=?, last_error=?, updated_at=?
		WHERE id=? AND status=? AND sha256=? AND size=? AND mtime_ns=? AND upload_id=? AND asset_id=?`,
		Uploading, assetID, "RECEIPT_PENDING", now(), f.ID, f.Status, f.SHA256, f.Size, f.MtimeNs,
		f.UploadID, f.AssetID)
	if err != nil {
		return err
	}
	if err := oneFile(result); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value`,
		fmt.Sprintf("receipt_pending:%d", f.ID), "true"); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) ReceiptPending(ctx context.Context, id int64) (bool, error) {
	v, err := s.KV(ctx, fmt.Sprintf("receipt_pending:%d", id))
	return v == "true", err
}

// MarkDoneWithReceipt는 검증된 영수증과 완료 상태를 같은 SQLite 트랜잭션으로 보관한다.
func (s *Store) MarkDoneWithReceipt(ctx context.Context, f *File, receipt string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }() // Commit 뒤에는 ErrTxDone만 돌려준다
	result, err := tx.ExecContext(ctx, `UPDATE files SET status=?, last_error='', updated_at=?
		WHERE id=? AND status=? AND sha256=? AND size=? AND mtime_ns=? AND upload_id=? AND asset_id=?`,
		Done, now(), f.ID, Uploading, f.SHA256, f.Size, f.MtimeNs, f.UploadID, f.AssetID)
	if err != nil {
		return err
	}
	if err := oneFile(result); err != nil {
		return err
	}
	for key, value := range map[string]string{
		fmt.Sprintf("receipt:%d", f.ID): receipt,
		"last_success_at":               s.Now().UTC().Format(time.RFC3339),
	} {
		if _, err = tx.ExecContext(ctx, `INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value`,
			key, value); err != nil {
			return err
		}
	}
	if _, err = tx.ExecContext(ctx, `DELETE FROM kv WHERE key=?`, fmt.Sprintf("receipt_pending:%d", f.ID)); err != nil {
		return err
	}
	return tx.Commit()
}

// Reset은 업로드 세션을 버리고 처음부터 다시 보내게 한다 (세션 만료 410 등).
func (s *Store) Reset(ctx context.Context, id int64) error {
	_, err := s.db.ExecContext(ctx, `UPDATE files SET status=?, upload_id='', updated_at=? WHERE id=?`, Queued, now(), id)
	return err
}

func (s *Store) MarkStatus(ctx context.Context, id int64, status, reason string) error {
	_, err := s.db.ExecContext(ctx, `UPDATE files SET status=?, last_error=?, updated_at=? WHERE id=?`, status,
		reason, now(), id)
	return err
}

// Retry는 다음 시도 시각을 미룬다 (지수 백오프는 호출자가 계산).
func (s *Store) Retry(ctx context.Context, id int64, reason string, after time.Duration) error {
	_, err := s.db.ExecContext(ctx, `UPDATE files SET attempts=attempts+1, last_error=?, next_attempt_at=?,
		updated_at=? WHERE id=?`, reason, s.Now().Add(after).UnixNano(), now(), id)
	return err
}

type SourceQueue struct {
	DeviceID string
	Items    int
	Bytes    int64
}

type Stats struct {
	QueueItems int
	QueueBytes int64
	Excluded   int
	Failed     int
	Done       int
	Sources    []SourceQueue
}

func (s *Store) Stats(ctx context.Context) (Stats, error) {
	var st Stats
	rows, err := s.db.QueryContext(ctx, `SELECT source_device_id, status, count(*), coalesce(sum(size),0) FROM files
		GROUP BY source_device_id, status ORDER BY source_device_id`)
	if err != nil {
		return st, err
	}
	defer func() { _ = rows.Close() }() // 반복 오류는 rows.Err()로 확인한다
	per := map[string]*SourceQueue{}
	var order []string
	for rows.Next() {
		var dev, status string
		var n int
		var b int64
		if err := rows.Scan(&dev, &status, &n, &b); err != nil {
			return st, err
		}
		switch status {
		case Queued, Uploading:
			st.QueueItems += n
			st.QueueBytes += b
			if per[dev] == nil {
				per[dev] = &SourceQueue{DeviceID: dev}
				order = append(order, dev)
			}
			per[dev].Items += n
			per[dev].Bytes += b
		case Excluded:
			st.Excluded += n
		case Failed:
			st.Failed += n
		case Done:
			st.Done += n
		}
	}
	for _, d := range order {
		st.Sources = append(st.Sources, *per[d])
	}
	return st, rows.Err()
}

// ExcludeDisallowed는 정책이 바뀌어 더 이상 허용되지 않는 대기 파일을 '정책 제외'로 표시한다.
func (s *Store) ExcludeDisallowed(ctx context.Context, allowed func(f *File) (bool, string)) (int, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT `+cols+` FROM files WHERE status = ?`, Queued)
	if err != nil {
		return 0, err
	}
	var victims []struct {
		id     int64
		reason string
	}
	for rows.Next() {
		f, err := scan(rows)
		if err != nil {
			_ = rows.Close()
			return 0, err
		}
		if ok, reason := allowed(f); !ok {
			victims = append(victims, struct {
				id     int64
				reason string
			}{f.ID, reason})
		}
	}
	_ = rows.Close()
	for _, v := range victims {
		if err := s.MarkStatus(ctx, v.id, Excluded, v.reason); err != nil {
			return 0, err
		}
	}
	return len(victims), nil
}

func (s *Store) SetKV(ctx context.Context, k, v string) error {
	_, err := s.db.ExecContext(ctx, `INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value`, k, v)
	return err
}

func (s *Store) KV(ctx context.Context, k string) (string, error) {
	var v string
	err := s.db.QueryRowContext(ctx, `SELECT value FROM kv WHERE key = ?`, k).Scan(&v)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("kv %s: %w", k, err)
	}
	return v, nil
}

// ---- 관측값 대기열

type QueuedObservation struct {
	ID   int64
	Item string // JSON (observation-batch 항목)
}

func (s *Store) EnqueueObservation(ctx context.Context, sourceDeviceID, itemJSON string) error {
	_, err := s.db.ExecContext(ctx, `INSERT INTO observations_queue (source_device_id, item, created_at) VALUES (?,?,?)`,
		sourceDeviceID, itemJSON, now())
	return err
}

// ObservationSources는 대기 관측값이 있는 원천 장비 목록이다.
func (s *Store) ObservationSources(ctx context.Context) ([]string, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT DISTINCT source_device_id FROM observations_queue ORDER BY 1`)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }() // 반복 오류는 rows.Err()로 확인한다
	var out []string
	for rows.Next() {
		var d string
		if err := rows.Scan(&d); err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// PeekObservations는 오래된 것부터 limit개를 돌려준다 (시간순 전송).
func (s *Store) PeekObservations(ctx context.Context, sourceDeviceID string, limit int) ([]QueuedObservation, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id, item FROM observations_queue WHERE source_device_id = ? ORDER BY id LIMIT ?`,
		sourceDeviceID, limit)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }() // 반복 오류는 rows.Err()로 확인한다
	var out []QueuedObservation
	for rows.Next() {
		var q QueuedObservation
		if err := rows.Scan(&q.ID, &q.Item); err != nil {
			return nil, err
		}
		out = append(out, q)
	}
	return out, rows.Err()
}

// DeleteObservations는 서버가 결과를 준 항목(ACCEPTED·DUPLICATE·REJECTED)을 지운다.
func (s *Store) DeleteObservations(ctx context.Context, ids []int64) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	for _, id := range ids {
		if _, err := tx.ExecContext(ctx, `DELETE FROM observations_queue WHERE id = ?`, id); err != nil {
			_ = tx.Rollback()
			return err
		}
	}
	return tx.Commit()
}

func (s *Store) ObservationQueueSize(ctx context.Context) (int, error) {
	var n int
	err := s.db.QueryRowContext(ctx, `SELECT count(*) FROM observations_queue`).Scan(&n)
	return n, err
}
