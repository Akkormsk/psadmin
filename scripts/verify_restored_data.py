"""Fingerprint original table columns in a newly restored, isolated verification database."""
import json
import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
django.setup()
from django.db import connection, transaction

name = connection.settings_dict['NAME']
if not name.startswith('verify_intake_') or connection.settings_dict.get('HOST') != 'test-db':
    raise SystemExit('Verification database guard failed')
mode = os.environ['VERIFY_MODE']
path = '/tmp/' + name + '.json'
quote = connection.ops.quote_name
with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
        cursor.execute("SET LOCAL statement_timeout = '120s'")
        if mode == 'before':
            baseline = {}
            for table in connection.introspection.table_names(cursor):
                if table == 'django_migrations':
                    continue
                columns = [column.name for column in connection.introspection.get_table_description(cursor, table)]
                maximum = None
                if 'id' in columns:
                    cursor.execute('SELECT max(id) FROM ' + quote(table))
                    maximum = cursor.fetchone()[0]
                    if not isinstance(maximum, int):
                        maximum = None
                baseline[table] = {'columns':columns,'max_id':maximum}
        elif mode == 'after':
            with open(path) as source:
                baseline = json.load(source)
        else:
            raise SystemExit('Unknown verification mode')
        mismatches, total = [], 0
        for table, metadata in baseline.items():
            columns = ','.join('t.' + quote(column) for column in metadata['columns'])
            restriction = ' WHERE id <= %s' if metadata['max_id'] is not None else ''
            params = [metadata['max_id']] if restriction else []
            sql = 'SELECT count(*), md5(COALESCE(string_agg(digest, \'\' ORDER BY digest),\'\')) FROM (SELECT md5((SELECT row_to_json(original) FROM (SELECT ' + columns + ') original)::text) AS digest FROM ' + quote(table) + ' t' + restriction + ') original_rows'
            cursor.execute(sql, params)
            count, digest = cursor.fetchone()
            total += count
            if mode == 'before':
                metadata.update(rows=count,digest=digest)
            elif count != metadata['rows'] or digest != metadata['digest']:
                mismatches.append({'table':table,'before':metadata['rows'],'after':count})
        if mode == 'before':
            with open(path, 'w') as target:
                json.dump(baseline,target)
        print(json.dumps({'mode':mode,'database':name,'tables':len(baseline),'original_rows':total,'mismatches':mismatches}),flush=True)
        if mismatches:
            raise SystemExit('Original data changed during migration rehearsal')
