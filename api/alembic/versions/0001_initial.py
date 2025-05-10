"""initial

Revision ID: 0001_initial
Revises: 
Create Date: 2025-05-10 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# уникальный ID миграции
revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    # создаём таблицу tenants
    op.create_table(
        'tenants',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('phone_id', sa.String(), unique=True, nullable=False),
        sa.Column('wh_token', sa.Text(), nullable=False),
        sa.Column('system_prompt', sa.Text(), server_default='You are a helpful assistant.'),
    )
    # создаём таблицу messages
    op.create_table(
        'messages',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('tenant_id', sa.String(), sa.ForeignKey('tenants.id'), index=True),
        sa.Column('wa_msg_id', sa.String(), unique=True),
        sa.Column('role', sa.Enum('user','assistant', name='role_enum')),
        sa.Column('text', sa.Text()),
        sa.Column('ts', sa.DateTime(), server_default=sa.func.now()),
    )

def downgrade():
    # откатываем в обратном порядке
    op.drop_table('messages')
    op.drop_table('tenants')
