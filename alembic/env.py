from alembic import context

from app.config import Settings
from app.db import make_engine
from app.models import Base


url = Settings.from_env().database_url
if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            # Python 3.11 sqlite3 otherwise runs DDL outside the transaction.
            connection.exec_driver_sql("BEGIN")
            context.configure(connection=connection, target_metadata=Base.metadata)
            context.run_migrations()
    finally:
        engine.dispose()
