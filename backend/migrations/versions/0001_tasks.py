"""Initial task, event and artifact tables."""
from alembic import op
import sqlalchemy as sa

revision = '0001_tasks'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('tasks',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('schema_version', sa.Integer, nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('active_slot', sa.Integer, unique=True, nullable=True),
        sa.Column('spec', sa.JSON, nullable=False),
        sa.Column('model', sa.String(120), nullable=False),
        sa.Column('summary', sa.JSON, nullable=False),
        sa.Column('created_at', sa.String(40), nullable=False),
        sa.Column('updated_at', sa.String(40), nullable=False))
    op.create_index('ix_tasks_created', 'tasks', ['created_at'])
    op.create_table('task_events',
        sa.Column('task_id', sa.String(36), sa.ForeignKey('tasks.id'), primary_key=True),
        sa.Column('seq', sa.Integer, primary_key=True),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('created_at', sa.String(40), nullable=False))
    op.create_table('artifacts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('task_id', sa.String(36), sa.ForeignKey('tasks.id'), nullable=False),
        sa.Column('filename', sa.String(80), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('size_bytes', sa.Integer, nullable=False),
        sa.UniqueConstraint('task_id', 'filename'))
    op.create_index('ix_artifacts_task', 'artifacts', ['task_id'])


def downgrade():
    op.drop_table('artifacts')
    op.drop_table('task_events')
    op.drop_table('tasks')
