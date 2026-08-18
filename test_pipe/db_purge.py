"""Разовая очистка накопленных записей (usage/forward/balances).

Курсы (token_rates) и настройки (settings) НЕ трогаются. Id-счётчики
сбрасываются, новые записи начнутся с 1. Перед удалением — бэкап файла БД.
"""
import shutil
import sqlite3
from datetime import date

DB = r'C:\ПРОЕКТЫ\Сервер списания кредитов\credits.db'
BACKUP = rf'C:\ПРОЕКТЫ\Сервер списания кредитов\credits.backup-{date.today():%Y-%m-%d}.db'

shutil.copy2(DB, BACKUP)
print('backup:', BACKUP)

db = sqlite3.connect(DB, timeout=15)
try:
    db.execute('BEGIN')
    db.execute('DELETE FROM forward_log')       # сначала дочерняя
    db.execute('DELETE FROM usage_records')
    db.execute('DELETE FROM user_balances')
    db.execute("DELETE FROM sqlite_sequence WHERE name IN ('usage_records','forward_log')")
    db.commit()
except Exception:
    db.rollback()
    raise

for t in ('usage_records', 'forward_log', 'user_balances', 'token_rates', 'settings'):
    cnt = db.execute(f'select count(*) from [{t}]').fetchone()[0]
    print(f'{t:<16} {cnt} строк')
print('OK — база очищена, курсы и настройки сохранены')
