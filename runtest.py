#!/usr/bin/env python
'''
This script will run the tests from the requirements repository (from t/) and
output the result of the run in the same format (into out/).

Usage: ./runtest.py [-x] [<test> ...]

If no tests are specified, all tests found in t/ will be run.

-x will cause the testing to stop at the first failure

-p automatically add zones to a pdns output (for every test where outputs are not used) and compare
   dim and pdns for every zone change (exiting if a difference is detected)
'''


import errno
import logging
import os.path
import re
import shlex
import sys
from io import StringIO
from itertools import zip_longest
from subprocess import Popen, PIPE

from dimcli import CLI, config
from dimclient import DimClient

from tests.pdns_util import diff_files, test_pdns_output_process, setup_pdns_output, compare_dim_pdns_zones


topdir = os.path.dirname(os.path.abspath(__file__))
T_DIR = os.path.join(topdir, 't')
OUT_DIR = os.path.join(topdir, 'out')
PDNS_ADDRESS = '127.1.1.1'
PDNS_DB_URI = 'mysql://pdns:pdns@127.0.0.1:3307/pdns1'
DIM_MYSQL_OPTIONS = '-h127.0.0.1 -P3307 -udim -pdim dim'
DIM_MYSQL_COMMAND = 'mysql ' + DIM_MYSQL_OPTIONS


server = None
pdns_output_proc = None


class PDNSOutputProcess(object):
    def __init__(self, needed):
        self.needed = needed

    def __enter__(self):
        if self.needed:
            self.proc = test_pdns_output_process(False)
        return self

    def __exit__(self, *args):
        if self.needed:
            self.proc.kill()
            self.proc = None

    def wait_updates(self):
        '''Wait for all updates to be processed'''
        if self.needed:
            while True:
                out = Popen(DIM_MYSQL_COMMAND, shell=True, stdin=PIPE, stdout=PIPE)\
                      .communicate(input='SELECT COUNT(*) FROM outputupdate')[0]
                if int(out.split()[1]) == 0:
                    break
                else:
                    os.read(self.proc.stdout.fileno(), 1024)


def is_ignorable(line):
    return len(line.strip()) == 0 or line.startswith('#')


def generates_table(line):
    return line.startswith('$ ndcli list') or line.startswith('$ ndcli dump zone') or line.startswith('$ ndcli history')


def generates_map(line):
    return line.startswith('$ ndcli show') or line.startswith('$ ndcli modify rr') \
        or re.search('(get|mark) (ip|delegation)', line) or re.search('ndcli create rr .* from', line)


def is_pdns_query(line):
    return any(cmd in line for cmd in ('dig', 'drill'))


def _ndcli(cmd, cmd_input=None):
    root_logger = logging.getLogger()
    for h in root_logger.handlers:
        root_logger.removeHandler(h)
    stderr = StringIO()
    stderrHandler = logging.StreamHandler(stderr)
    stderrHandler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
    stderrHandler.setLevel(logging.DEBUG)
    root_logger.addHandler(stderrHandler)
    old_stdout = sys.stdout
    import codecs
    sys.stdout = stdout = codecs.getwriter('utf8')(StringIO())
    if cmd_input is not None:
        old_stdin = sys.stdin
        sys.stdin = StringIO(cmd_input)
    CLI().run(['ndcli'] + cmd)
    if cmd_input is not None:
        sys.stdin = old_stdin
    sys.stdout = old_stdout
    root_logger.removeHandler(stderrHandler)
    return stdout.getvalue(), stderr.getvalue()


def clean_database():
    commands = [
        'echo "delete from domains; delete from records;" | mysql -h127.0.0.1 -P3307 -updns -ppdns pdns1',
        'echo "delete from domains; delete from records;" | mysql -h127.0.0.1 -P3307 -updns -ppdns pdns2']
    clean_sql = os.path.join(topdir, 'clean.sql')
    if not hasattr(clean_database, 'dumped'):
        commands.extend([
            "echo 'drop database dim; create database dim;' | " + DIM_MYSQL_COMMAND,
            '/opt/dim/bin/manage_db clear -t',
            'mysqldump ' + DIM_MYSQL_OPTIONS + ' >' + clean_sql])
        clean_database.dumped = True
    else:
        commands.extend([DIM_MYSQL_COMMAND + ' <' + clean_sql])
    if os.system(';'.join(commands)) != 0:
        sys.exit(1)


def run_command(line, cmd_input=None):
    cmd = shlex.split(line[7:])
    # HACK shell-style redirection
    redir_out = None
    if '>' in cmd:
        idx = cmd.index('>')
        redir_out = cmd[idx + 1]
        cmd = cmd[:idx]
    out, err = _ndcli(cmd, cmd_input)
    if redir_out:
        with open(redir_out, 'w') as f:
            f.write(out)
        out = ''
    return out + err


def run_system_command(*args, **kwargs):
    kwargs.setdefault('shell', True)
    kwargs['close_fds'] = True
    kwargs['stdout'] = PIPE
    kwargs['stderr'] = PIPE
    sp = Popen(*args, **kwargs)
    stdout, stderr = sp.communicate()
    return stdout.splitlines() + stderr.splitlines()


def process_command(actual_output, expected_output, out, sort_before=False):
    passed = True
    if sort_before:
        actual_output.sort()
        expected_output.sort()
    for actual, expected in zip_longest(actual_output, expected_output, fillvalue=''):
        expected = expected.strip('\n')
        if expected.endswith(' re'):
            if re.match(expected[:-3], actual):
                out.write(expected + '\n')
            else:
                out.write(actual + '\n')
                passed = False
        else:
            out.write(actual + '\n')
            if (actual != expected):
                passed = False
    return passed


def table_from_lines(lines, cmd):
    if generates_map(cmd):
        result = []
        for line in lines:
            if not is_ignorable(line):
                result.append(line.split(':', 1))
        return result
    elif ' -H' in cmd or 'dump zone' in cmd:
        result = []
        for line in lines:
            if is_ignorable(line):
                continue
            result.append(line.split('\t'))
        return result
    else:
        result = []
        while lines and is_ignorable(lines[0]):
            lines.pop(0)
        if not lines:
            return result
        headers = lines[0].split()
        offsets = []
        for header in headers:
            # some headers are substrings of others
            start_find = offsets[-1] + 1 if offsets else 0
            offsets.append(lines[0].find(header, start_find))
        for line in lines:
            if is_ignorable(line):
                continue
            row = [''] * len(offsets)
            for col, offset in enumerate(offsets):
                if offset < len(line):
                    next_offset = len(line)
                    if col < len(offsets) - 1:
                        next_offset = min(offsets[col + 1], len(line))
                    row[col] = line[offset:next_offset].strip()
            result.append(row)
        return result


def add_regex(table, cmd):
    def check_regexes(table, regexes):
        regexes = [re.compile(r) for r in regexes]
        for row in table:
            for regex in regexes:
                if re.search(regex, row[0]):
                    row[0] = regex
                    break

    if generates_map(cmd):
        for (no, row) in enumerate(table):
            if row[0] in ['created', 'modified']:
                table[no][1] = re.compile('.*')
            elif row[0] in ['created_by', 'modified_by']:
                table[no][1] = re.compile('.*')
    elif generates_table(cmd):
        if re.search('list zone .* keys', cmd):
            for row in table:
                row[0] = re.compile('.*_[zk]sk_.*')
                row[2] = re.compile('\d*')
                row[5] = re.compile('.*')
        if re.search('list zone .* dnskeys', cmd):
            for row in table:
                row[0] = re.compile('\d*')
                row[1] = re.compile('\d*')
                row[3] = re.compile('.*')
        if re.search('list zone .* keys', cmd):
            for row in table:
                row[0] = re.compile('.*')
                row[2] = re.compile('\d*')
                row[5] = re.compile('.*')
        if re.search('list zone .* ds', cmd):
            for row in table[1:]:
                row[0] = re.compile('\d*')
                row[2] = re.compile('\d')
                row[3] = re.compile('.*')
        if 'dcli list zone' in cmd or 'dcli dump zone' in cmd or 'dcli list rrs' in cmd:
            for rr_row in table:
                for rr_col in [2, 3, 4]:
                    if (rr_col + 1) < len(rr_row):
                        if rr_row[rr_col] == 'SOA':
                            stuff = rr_row[rr_col + 1].split()
                            rr_row[rr_col + 1] = re.compile('%s %s \d+ \d+ \d+ \d+ \d+' % (stuff[0], stuff[1]))
                        elif rr_row[rr_col] == 'DS':
                            rr_row[rr_col + 1] = re.compile('\d+ 8 2 .*')
        elif 'dcli history' in cmd:
            for row in table:
                if row[0] != 'timestamp':
                    row[0] = re.compile('.*')
                if row[-1].startswith('set_attr serial='):
                    row[-1] = re.compile('set_attr serial=\d+')
                if row[4] == 'key':
                    row[5] = re.compile('.*')
    elif 'dnssec enable' in cmd or 'dnssec new' in cmd:
        check_regexes(table, ['.*Created key .*_[zk]sk_.* for zone (.*)',
                              '.*Creating RR .* DS \d+ 8 2 .* in zone .*'])
    elif 'dnssec disable' in cmd or 'dnssec delete' in cmd or 'delete zone' in cmd:
        check_regexes(table, ['.*Deleting RR .* DS \d+ 8 2 .* from zone .*'])
    return table


def match_table(actual_table, expected_table, actual_raw, expected_raw):
    def match(row, info):
        if len(row) != len(info):
            return False
        for i in range(len(row)):
            if type(info[i]) == str:
                if info[i] != row[i]:
                    return False
            else:
                if re.match(info[i], row[i]) is None:
                    return False
        return True
    matched = {}
    for no, row in enumerate(actual_table):
        for expected_no, expected_row in enumerate(expected_table):
            if expected_no not in list(matched.values()) and match(row, expected_row):
                matched[no] = expected_no
                break
    result = []
    expected_matched = [expected_raw[i] for i in sorted(matched.values())]
    for no, row in enumerate(actual_raw):
        if no in matched:
            result.append(expected_matched.pop(0))
        else:
            result.append(row)
    return result


def split_cat_command(line):
    return 'EOF', '$' + line.split('|')[1]


def get_cat_input(lines, word, out):
    cat_input = ''
    while len(lines) > 0:
        line = lines.pop(0)
        out.write(line)
        if line == word + '\n':
            break
        else:
            cat_input += line
    return cat_input


def check_pdns_output(line, out):
    zone_commands = [c + o
                     for c in ('create ', 'delete ', 'modify ')
                     for o in ('rr', 'zone', 'zone-profile', 'zone-group')]
    if not any(line[8:].startswith(cmd) for cmd in zone_commands):
        return True
    zone_view_map = setup_pdns_output(server)
    pdns_output_proc.wait_updates()
    if not compare_dim_pdns_zones(server, PDNS_ADDRESS, zone_view_map):
        out.write("Zone incorrectly exported\n")
        return False
    return True


def run_test(testfile, outfile, stop_on_error=False, auto_pdns_check=False):
    with open(testfile, 'r') as f:
        lines = f.readlines()
    # ignore auto_pdns_check if zone-groups or outputs are involved
    pdns_needed = False
    for line in lines:
        if 'create zone-group' in line or 'create output' in line:
            auto_pdns_check = False
        if 'drill' in line or 'dig' in line:
            pdns_needed = True
    if auto_pdns_check:
        pdns_needed = True

    global pdns_output_proc
    with PDNSOutputProcess(pdns_needed) as pdns_output_proc:
        clean_database()
        run_command('$ ndcli login -u admin -p p')
        if auto_pdns_check:
            global server
            server = DimClient(config['server'], cookie_file=os.path.expanduser('~/.ndcli.cookie'))
            server.output_create('pdns_output', 'pdns-db', db_uri=PDNS_DB_URI)
            server.zone_group_create('pdns_group')
            server.output_add_group('pdns_output', 'pdns_group')
        with open(outfile, 'w') as out:
            while len(lines) > 0:
                line = lines.pop(0)
                out.write(line)
                cmd_input = None
                if line.startswith('$ cat <<EOF | ndcli'):
                    word, line = split_cat_command(line)
                    cmd_input = get_cat_input(lines, word, out)

                if line.startswith('$ '):
                    expected_result = []
                    while len(lines) > 0 and not lines[0].startswith('$ '):
                        expected_result.append(lines.pop(0))
                    while len(expected_result) > 0 and is_ignorable(expected_result[-1]):
                        lines[0:0] = expected_result.pop()
                    if line.startswith('$ ndcli'):
                        result = run_command(line, cmd_input)

                        if generates_table(line) or generates_map(line):
                            actual_table = table_from_lines(result.split('\n'), line)
                            expected_table = table_from_lines([x.strip('\n') for x in expected_result], line)
                        else:
                            actual_table = [[x] for x in result.splitlines(True)]
                            expected_table = [[x] for x in expected_result]
                        expected_table = add_regex(expected_table, line)
                        output = match_table(actual_table,
                                             expected_table,
                                             result.splitlines(True),
                                             expected_result)
                        out.writelines(output)

                        ok = output == expected_result
                        if auto_pdns_check and not check_pdns_output(line, out):
                            ok = False
                    else:
                        line = line.strip('\n')
                        if is_pdns_query(line):
                            pdns_output_proc.wait_updates()
                        result = run_system_command(line[2:])
                        ok = process_command(result, expected_result, out, is_pdns_query(line))
                    if stop_on_error and not ok:
                        return False
        return not os.system('diff %s %s >/dev/null' % (testfile, outfile))


if __name__ == '__main__':
    stop_on_error = False
    auto_pdns_check = False
    run_diff = False
    diff_left = OUT_DIR
    diff_right = T_DIR
    tests = []
    for test in sys.argv[1:]:
        if test == '-x':
            stop_on_error = True
        elif test == '-p':
            auto_pdns_check = True
        elif test == '-d':
            run_diff = True
        else:
            tests.append(test)
    if not tests:
        tests = sorted(os.listdir(T_DIR))
    failed = 0

    try:
        os.makedirs(OUT_DIR)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise

    for test in tests:
        testfile = os.path.join(T_DIR, test)
        outfile = os.path.join(OUT_DIR, test)
        print('%s ... ' % test, end='')
        sys.stdout.flush()
        try:
            ok = run_test(testfile, outfile, stop_on_error, auto_pdns_check)
        except Exception:
            logging.exception('')
            ok = False
        print('ok' if ok else 'fail')
        sys.stdout.flush()
        if not ok:
            failed += 1
            if stop_on_error or len(tests) == 1:
                diff_left = outfile
                diff_right = testfile
            if stop_on_error:
                break
    if failed and run_diff:
        diff_files(diff_left, diff_right)
    sys.exit(1 if failed else 0)
