import re

BLOCKED_KEYWORDS = {
    'DROP', 'ALTER', 'CREATE', 'INSERT', 'UPDATE', 'DELETE',
    'TRUNCATE', 'REPLACE', 'RENAME', 'GRANT', 'REVOKE',
    'LOAD_FILE', 'INTO OUTFILE', 'INTO DUMPFILE',
    'SLEEP', 'BENCHMARK', 'WAITFOR', 'DELAY',
    'INFORMATION_SCHEMA', 'PERFORMANCE_SCHEMA',
    'EXEC', 'EXECUTE', 'EXECUTE IMMEDIATE',
    'XP_CMDSHELL', 'SP_OACREATE', 'SP_OAMETHOD',
    'UNION', 'UNION ALL',
}

DANGEROUS_FUNCTIONS = {
    'LOAD_FILE', 'INTO OUTFILE', 'INTO DUMPFILE',
    'BENCHMARK', 'SLEEP', 'WAITFOR DELAY',
    'XP_CMDSHELL', 'SP_OACREATE', 'SP_OAMETHOD',
    'DBMS_PIPE', 'UTL_FILE', 'UTL_HTTP',
}


def validate_sql_safety(sql: str) -> tuple:
    if not sql or not sql.strip():
        return False, "SQL 语句不能为空"

    cleaned_sql = _remove_comments(sql)

    first_keyword = _get_first_keyword(cleaned_sql)
    if first_keyword != 'SELECT':
        return False, f"只允许 SELECT 查询，检测到: {first_keyword or '空语句'}"

    upper_sql = cleaned_sql.upper()

    for kw in BLOCKED_KEYWORDS:
        if kw in upper_sql:
            if kw in ('UNION', 'UNION ALL'):
                continue
            return False, f"禁止使用关键字: {kw}"

    for fn in DANGEROUS_FUNCTIONS:
        if fn in upper_sql:
            return False, f"禁止使用危险函数: {fn}"

    semicolons = [i for i, c in enumerate(cleaned_sql) if c == ';']
    if len(semicolons) > 1:
        return False, "禁止执行多条 SQL 语句"
    if len(semicolons) == 1 and semicolons[0] != len(cleaned_sql) - 1:
        return False, "禁止执行多条 SQL 语句"

    return True, "OK"


def _remove_comments(sql: str) -> str:
    sql = re.sub(r'--.*$', '', sql, flags=re.MULTILINE)
    sql = re.sub(r'/\*.*?\*/', '', sql, flags=re.DOTALL)
    sql = re.sub(r'#.*$', '', sql, flags=re.MULTILINE)
    return sql.strip()


def _get_first_keyword(sql: str) -> str:
    stripped = sql.strip()
    if not stripped:
        return ''
    match = re.match(r'([A-Za-z]+)', stripped)
    return match.group(1).upper() if match else ''
