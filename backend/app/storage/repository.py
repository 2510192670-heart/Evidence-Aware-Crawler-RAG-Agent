from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError


TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted'}
STAGES = ['created', 'observing', 'analyzing', 'executing', 'verifying']
ARTIFACT_NAMES = {'evidence.json', 'cloud_payload.json', 'plan.json', 'result.json', 'report.json', 'report.md', 'retrieval.json',
                  'collector.py', 'collector_result.json', 'collector_verification.json', 'repair.json', 'failure_retrieval.json'}


class BusyError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.engine = sa.create_engine(sa.URL.create('sqlite', database=str(self.root / 'agent.sqlite3')),
                                       connect_args={'check_same_thread': False, 'timeout': 10})
        @sa.event.listens_for(self.engine, 'connect')
        def setup(connection, record):
            connection.execute('PRAGMA foreign_keys=ON')

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        cfg = Config()
        cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[2] / 'migrations'))
        with self.engine.begin() as connection:
            cfg.attributes['connection'] = connection
            command.upgrade(cfg, 'head')
        metadata = sa.MetaData()
        self.tasks = sa.Table('tasks', metadata, autoload_with=self.engine)
        self.task_events = sa.Table('task_events', metadata, autoload_with=self.engine)
        self.artifact_table = sa.Table('artifacts', metadata, autoload_with=self.engine)

    @contextmanager
    def write(self):
        with self.engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def public(row):
        result = dict(row)
        result.pop('active_slot', None)
        return result

    def _event(self, connection, task_id, status):
        seq = connection.scalar(sa.select(sa.func.coalesce(sa.func.max(self.task_events.c.seq), 0))
                                .where(self.task_events.c.task_id == task_id)) + 1
        connection.execute(self.task_events.insert().values(task_id=task_id, seq=seq, status=status, created_at=now()))

    def create(self, spec, model):
        task_id = str(uuid.uuid4())
        timestamp = now()
        try:
            with self.write() as connection:
                connection.execute(self.tasks.insert().values(id=task_id, schema_version=1, status='created',
                    active_slot=1, spec=spec, model=model, summary={}, created_at=timestamp, updated_at=timestamp))
                self._event(connection, task_id, 'created')
        except IntegrityError:
            raise BusyError('task_already_running') from None
        return self.get(task_id)

    def get(self, task_id):
        with self.engine.connect() as connection:
            row = connection.execute(sa.select(self.tasks).where(self.tasks.c.id == task_id)).mappings().first()
        return self.public(row) if row else None

    def list_tasks(self, page=1, page_size=20):
        with self.engine.connect() as connection:
            rows = connection.execute(sa.select(self.tasks).order_by(self.tasks.c.created_at.desc())
                                      .offset((page - 1) * page_size).limit(page_size)).mappings().all()
            total = connection.scalar(sa.select(sa.func.count()).select_from(self.tasks))
        return {'items': [self.public(row) for row in rows], 'total': total, 'page': page, 'page_size': page_size}

    def transition(self, task_id, status, summary=None):
        if status not in set(STAGES) | TERMINAL | {'cancelling'}:
            raise ValueError('unknown_status')
        with self.write() as connection:
            row = connection.execute(sa.select(self.tasks).where(self.tasks.c.id == task_id)).mappings().first()
            if row is None:
                return None
            old = row['status']
            if old in TERMINAL or status == old:
                return self.public(row)
            if old == 'cancelling' and status not in {'cancelled', 'interrupted'}:
                if status in TERMINAL:
                    status = 'cancelled'
                    summary = {**(summary or {}), 'status': 'cancelled'}
                else:
                    return self.public(row)
            if old in STAGES and status in STAGES and STAGES.index(status) <= STAGES.index(old):
                return self.public(row)
            values = {'status': status, 'updated_at': now(), 'active_slot': None if status in TERMINAL else 1}
            if summary is not None:
                values['summary'] = summary
            connection.execute(self.tasks.update().where(self.tasks.c.id == task_id).values(**values))
            self._event(connection, task_id, status)
        return self.get(task_id)

    def events(self, task_id, after_seq=0):
        with self.engine.connect() as connection:
            rows = connection.execute(sa.select(self.task_events).where(
                self.task_events.c.task_id == task_id, self.task_events.c.seq > after_seq)
                .order_by(self.task_events.c.seq).limit(100)).mappings().all()
        return [dict(row) for row in rows]

    def recover(self):
        with self.engine.connect() as connection:
            ids = connection.scalars(sa.select(self.tasks.c.id).where(self.tasks.c.active_slot == 1)).all()
        for task_id in ids:
            self.transition(task_id, 'interrupted', {'status': 'interrupted', 'error': 'process_interrupted'})

    def register_artifacts(self, task_id):
        folder = (self.root / 'tasks' / task_id).resolve()
        if not folder.is_relative_to(self.root / 'tasks'):
            raise ValueError('invalid_artifact_path')
        entries = []
        for name in sorted(ARTIFACT_NAMES):
            path = folder / name
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(folder):
                continue
            if path.stat().st_size > 16 * 1024 * 1024:
                continue
            raw = path.read_bytes()
            entries.append(dict(id=str(uuid.uuid4()), task_id=task_id, filename=name,
                                sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)))
        with self.write() as connection:
            for entry in entries:
                exists = connection.scalar(sa.select(self.artifact_table.c.id).where(
                    self.artifact_table.c.task_id == task_id, self.artifact_table.c.filename == entry['filename']))
                if not exists:
                    connection.execute(self.artifact_table.insert().values(**entry))

    def artifacts(self, task_id):
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(sa.select(self.artifact_table)
                .where(self.artifact_table.c.task_id == task_id).order_by(self.artifact_table.c.filename)).mappings()]

    def artifact(self, artifact_id):
        with self.engine.connect() as connection:
            row = connection.execute(sa.select(self.artifact_table).where(self.artifact_table.c.id == artifact_id)).mappings().first()
        return dict(row) if row else None

    def read_artifact(self, entry):
        folder = (self.root / 'tasks' / entry['task_id']).resolve()
        path = (folder / entry['filename']).resolve()
        if not folder.is_relative_to(self.root / 'tasks') or not path.is_relative_to(folder):
            raise ValueError('invalid_artifact_path')
        if not path.is_file() or path.stat().st_size != entry['size_bytes']:
            raise ValueError('artifact_changed')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('artifact_changed')
        return raw

    def close(self):
        self.engine.dispose()
