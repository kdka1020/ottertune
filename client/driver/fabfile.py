#
# OtterTune - fabfile.py
#
# Copyright (c) 2017-18, Carnegie Mellon University Database Group
#
'''
Created on Mar 23, 2018

@author: bohan
'''
import glob
import json
import os
import re
import time
from collections import OrderedDict
from multiprocessing import Process

import logging
from logging.handlers import RotatingFileHandler

import requests
from fabric.api import env, local, task, lcd
from fabric.state import output as fabric_output

import driver_config as dconf


def _setup():
    global LOGIN

    # Determine correct login for the type of host connection
    if dconf.HOST_CONN == 'local':
        LOGIN = 'localhost'

    elif dconf.HOST_CONN == 'remote':
        if not dconf.LOGIN_HOST:
            raise ValueError("LOGIN_HOST must be set if HOST_CONN=remote")

        LOGIN = dconf.LOGIN_HOST
        if dconf.LOGIN_NAME:
            LOGIN = '{}@{}'.format(dconf.LOGIN_NAME, LOGIN)

        if dconf.LOGIN_PORT:
            LOGIN += ':{}'.format(dconf.LOGIN_PORT)

    elif dconf.HOST_CONN == 'docker':
        if not dconf.CONTAINER_NAME:
            raise ValueError("CONTAINER_NAME must be set if HOST_CONN=docker")
        LOGIN = 'localhost'

    else:
        raise ValueError(("Invalid HOST_CONN: {}. Valid values are "
                          "'local', 'remote', or 'docker'.").format(dconf.HOST_CONN))

    # Create all output directories
    for d in (dconf.RESULT_DIR, dconf.LOG_DIR, dconf.DB_DUMP_DIR):
        os.makedirs(d, exist_ok=True)


_setup()


# Fabric environment settings
env.hosts = [LOGIN]
fabric_output.update({
    'running': True,
    'stdout': True,
})


# Configure logging
LOG = logging.getLogger(__name__)
LOG.setLevel(getattr(logging, dconf.LOG_LEVEL, logging.DEBUG))
Formatter = logging.Formatter(  # pylint: disable=invalid-name
    fmt='%(asctime)s [%(funcName)s:%(lineno)03d] %(levelname)-5s: %(message)s',
    datefmt='%m-%d-%Y %H:%M:%S')
ConsoleHandler = logging.StreamHandler()  # pylint: disable=invalid-name
ConsoleHandler.setFormatter(Formatter)
LOG.addHandler(ConsoleHandler)
FileHandler = RotatingFileHandler(  # pylint: disable=invalid-name
    dconf.DRIVER_LOG, maxBytes=50000, backupCount=2)
FileHandler.setFormatter(Formatter)
LOG.addHandler(FileHandler)


def _parse_bool(value):
    if not isinstance(value, bool):
        value = str(value).lower() == 'true'
    return value


@task
def check_disk_usage():
    partition = dconf.DATABASE_DISK
    disk_use = 0
    if partition:
        cmd = "df -h {}".format(partition)
        out = local(cmd, capture=True).splitlines()[1]
        m = re.search(r'\d+(?=%)', out)
        if m:
            disk_use = int(m.group(0))
        LOG.info("Current Disk Usage: %s%s", disk_use, '%')
    return disk_use


@task
def check_memory_usage():
    cmd = 'free -m -h'
    local(cmd)


@task
def create_controller_config():
    if dconf.DB_TYPE == 'postgres':
        dburl_fmt = 'jdbc:postgresql://localhost:5432/{db}'.format
    elif dconf.DB_TYPE == 'oracle':
        dburl_fmt = 'jdbc:oracle:thin:@localhost:1521:{db}'.format
    else:
        raise Exception("Database Type {} Not Implemented !".format(dconf.DB_TYPE))

    config = dict(
        database_type=dconf.DB_TYPE,
        database_url=dburl_fmt(db=dconf.DB_NAME),
        username=dconf.DB_USER,
        password=dconf.DB_PASSWORD,
        upload_code='DEPRECATED',
        upload_url='DEPRECATED',
        workload_name=dconf.OLTPBENCH_BENCH
    )

    with open(dconf.CONTROLLER_CONFIG, 'w') as f:
        json.dump(config, f, indent=2)


@task
def restart_database():
    if dconf.DB_TYPE == 'postgres':
        cmd = 'sudo -u postgres pg_ctl -D {} -w -t 600 restart -m fast'.format(dconf.PG_DATADIR)
    elif dconf.DB_TYPE == 'oracle':
        cmd = 'sh oracleScripts/shutdownOracle.sh && sh oracleScripts/startupOracle.sh'
    else:
        raise Exception("Database Type {} Not Implemented !".format(dconf.DB_TYPE))
    local(cmd)


@task
def drop_database():
    if dconf.DB_TYPE == 'postgres':
        cmd = "PGPASSWORD={} dropdb -e --if-exists {} -U {}".\
              format(dconf.DB_PASSWORD, dconf.DB_NAME, dconf.DB_USER)
    else:
        raise Exception("Database Type {} Not Implemented !".format(dconf.DB_TYPE))
    local(cmd)


@task
def create_database():
    if dconf.DB_TYPE == 'postgres':
        cmd = "PGPASSWORD={} createdb -e {} -U {}".\
              format(dconf.DB_PASSWORD, dconf.DB_NAME, dconf.DB_USER)
    else:
        raise Exception("Database Type {} Not Implemented !".format(dconf.DB_TYPE))
    local(cmd)


@task
def reset_conf():
    change_conf()


@task
def change_conf(next_conf=None):
    signal = "# configurations recommended by ottertune:\n"
    next_conf = next_conf or {}

    with open(dconf.DB_CONF, 'r') as f:
        lines = f.readlines()

    if signal not in lines:
        lines += ['\n', signal]

    signal_idx = lines.index(signal)
    lines = lines[0:signal_idx + 1]
    if dconf.BASE_DB_CONF:
        assert isinstance(dconf.BASE_DB_CONF, dict)
        base_conf = ['{} = {}\n'.format(*c) for c in sorted(dconf.BASE_DB_CONF.items())]
        lines.extend(base_conf)

    if isinstance(next_conf, str):
        with open(next_conf, 'r') as f:
            recommendation = json.load(
                f, encoding="UTF-8", object_pairs_hook=OrderedDict)['recommendation']
    else:
        recommendation = next_conf

    assert isinstance(recommendation, dict)

    for name, value in recommendation.items():
        if dconf.DB_TYPE == 'oracle' and isinstance(value, str):
            value = value.strip('B')
        lines.append('{} = {}\n'.format(name, value))
    lines.append('\n')

    tmpconf = 'tmp_' + os.path.basename(dconf.DB_CONF)
    with open(tmpconf, 'w') as f:
        f.write(''.join(lines))

    local('sudo cp {0} {0}.ottertune.bak'.format(dconf.DB_CONF))
    local('sudo mv {} {}'.format(tmpconf, dconf.DB_CONF))


@task
def load_oltpbench():
    cmd = "./oltpbenchmark -b {} -c {} --create=true --load=true".\
          format(dconf.OLTPBENCH_BENCH, dconf.OLTPBENCH_CONFIG)
    with lcd(dconf.OLTPBENCH_HOME):  # pylint: disable=not-context-manager
        local(cmd)


@task
def run_oltpbench():
    cmd = "./oltpbenchmark -b {} -c {} --execute=true -s 5 -o outputfile".\
          format(dconf.OLTPBENCH_BENCH, dconf.OLTPBENCH_CONFIG)
    with lcd(dconf.OLTPBENCH_HOME):  # pylint: disable=not-context-manager
        local(cmd)


@task
def run_oltpbench_bg():
    cmd = "./oltpbenchmark -b {} -c {} --execute=true -s 5 -o outputfile > {} 2>&1 &".\
          format(dconf.OLTPBENCH_BENCH, dconf.OLTPBENCH_CONFIG, dconf.OLTPBENCH_LOG)
    with lcd(dconf.OLTPBENCH_HOME):  # pylint: disable=not-context-manager
        local(cmd)


@task
def run_controller():
    if not os.path.exists(dconf.CONTROLLER_CONFIG):
        create_controller_config()
    cmd = 'gradle run -PappArgs="-c {} -d output/" --no-daemon > {}'.\
          format(dconf.CONTROLLER_CONFIG, dconf.CONTROLLER_LOG)
    with lcd(dconf.CONTROLLER_HOME):  # pylint: disable=not-context-manager
        local(cmd)


@task
def signal_controller():
    pidfile = os.path.join(dconf.CONTROLLER_HOME, 'pid.txt')
    with open(pidfile, 'r') as f:
        pid = int(f.read())
    cmd = 'sudo kill -2 {}'.format(pid)
    with lcd(dconf.CONTROLLER_HOME):  # pylint: disable=not-context-manager
        local(cmd)


@task
def save_dbms_result():
    t = int(time.time())
    files = ['knobs.json', 'metrics_after.json', 'metrics_before.json', 'summary.json']
    for f_ in files:
        srcfile = os.path.join(dconf.CONTROLLER_HOME, 'output', f_)
        dstfile = os.path.join(dconf.RESULT_DIR, '{}__{}'.format(t, f_))
        local('cp {} {}'.format(srcfile, dstfile))
    return t


@task
def save_next_config(next_config, t=None):
    if not t:
        t = int(time.time())
    with open(os.path.join(dconf.RESULT_DIR, '{}__next_config.json'.format(t)), 'w') as f:
        json.dump(next_config, f, indent=2)
    return t


@task
def free_cache():
    cmd = 'sync; sudo bash -c "echo 1 > /proc/sys/vm/drop_caches"'
    local(cmd)


@task
def upload_result(result_dir=None, prefix=None, upload_code=None):
    result_dir = result_dir or os.path.join(dconf.CONTROLLER_HOME, 'output')
    prefix = prefix or ''
    upload_code = upload_code or dconf.UPLOAD_CODE

    files = {}
    for base in ('summary', 'knobs', 'metrics_before', 'metrics_after'):
        fpath = os.path.join(result_dir, prefix + base + '.json')

        # Replaces the true db version with the specified version to allow for
        # testing versions not officially supported by OtterTune
        if base == 'summary' and dconf.OVERRIDE_DB_VERSION:
            with open(fpath, 'r') as f:
                summary = json.load(f)
            summary['real_database_version'] = summary['database_version']
            summary['database_version'] = dconf.OVERRIDE_DB_VERSION
            with open(fpath, 'w') as f:
                json.dump(summary, f, indent=1)

        files[base] = open(fpath, 'rb')

    response = requests.post(dconf.WEBSITE_URL + '/new_result/', files=files,
                             data={'upload_code': upload_code})
    if response.status_code != 200:
        raise Exception('Error uploading result.\nStatus: {}\nMessage: {}\n'.format(
            response.status_code, response.content))

    for f in files.values():  # pylint: disable=not-an-iterable
        f.close()

    LOG.info(response.content)

    return response


@task
def get_result(max_time_sec=180, interval_sec=5, upload_code=None):
    max_time_sec = int(max_time_sec)
    interval_sec = int(interval_sec)
    upload_code = upload_code or dconf.UPLOAD_CODE
    url = dconf.WEBSITE_URL + '/query_and_get/' + upload_code
    elapsed = 0
    response_dict = None
    response = ''

    while elapsed <= max_time_sec:
        rsp = requests.get(url)
        response = rsp.content.decode()
        assert response != 'null'

        LOG.debug('%s [status code: %d, content_type: %s, elapsed: %ds]', response,
                  rsp.status_code, rsp.headers.get('content-type', ''), elapsed)

        if rsp.status_code == 200:
            # Success
            response_dict = json.loads(rsp.json(), object_pairs_hook=OrderedDict)
            break

        elif rsp.status_code == 202:
            # Not ready
            time.sleep(interval_sec)
            elapsed += interval_sec

        elif rsp.status_code == 400:
            # Failure
            raise Exception(
                "Failed to download the next config.\nStatus code: {}\nMessage: {}\n".format(
                    rsp.status_code, response))

        else:
            raise NotImplementedError(
                "Unhandled status code: '{}'.\nMessage: {}".format(rsp.status_code, response))

    if not response_dict:
        assert elapsed > max_time_sec, \
            'response={} but elapsed={}s <= max_time={}s'.format(
                response, elapsed, max_time_sec)
        raise Exception(
            'Failed to download the next config in {}s: {} (elapsed: {}s)'.format(
                max_time_sec, response, elapsed))

    LOG.info('Downloaded the next config in %ds: %s', elapsed, json.dumps(response_dict, indent=4))

    return response_dict


@task
def download_debug_info(pprint=False):
    pprint = _parse_bool(pprint)
    url = '{}/dump/{}'.format(dconf.WEBSITE_URL, dconf.UPLOAD_CODE)
    params = {'pp': int(True)} if pprint else {}
    rsp = requests.get(url, params=params)

    if rsp.status_code != 200:
        raise Exception('Error downloading debug info.')

    filename = rsp.headers.get('Content-Disposition').split('=')[-1]
    file_len, exp_len = len(rsp.content), int(rsp.headers.get('Content-Length'))
    assert file_len == exp_len, 'File {}: content length != expected length: {} != {}'.format(
        filename, file_len, exp_len)

    with open(filename, 'wb') as f:
        f.write(rsp.content)
    LOG.info('Downloaded debug info to %s', filename)

    return filename


@task
def add_udf():
    cmd = 'sudo python3 ./LatencyUDF.py ../controller/output/'
    local(cmd)


@task
def upload_batch(result_dir=None, sort=True, upload_code=None):
    result_dir = result_dir or dconf.RESULT_DIR
    sort = _parse_bool(sort)
    results = glob.glob(os.path.join(result_dir, '*__summary.json'))
    if sort:
        results = sorted(results)
    count = len(results)

    LOG.info('Uploading %d samples from %s...', count, result_dir)
    for i, result in enumerate(results):
        prefix = os.path.basename(result)
        prefix_len = os.path.basename(result).find('_') + 2
        prefix = prefix[:prefix_len]
        upload_result(result_dir=result_dir, prefix=prefix, upload_code=upload_code)
        LOG.info('Uploaded result %d/%d: %s__*.json', i + 1, count, prefix)


@task
def dump_database():
    db_file_path = os.path.join(dconf.DB_DUMP_DIR, dconf.DB_NAME + '.dump')
    if os.path.exists(db_file_path):
        LOG.info('%s already exists ! ', db_file_path)
        return False
    else:
        LOG.info('Dump database %s to %s', dconf.DB_NAME, db_file_path)
        # You must create a directory named dpdata through sqlplus in your Oracle database
        if dconf.DB_TYPE == 'oracle':
            cmd = 'expdp {}/{}@{} schemas={} dumpfile={}.dump DIRECTORY=dpdata'.format(
                'c##tpcc', 'oracle', 'orcldb', 'c##tpcc', 'orcldb')
        elif dconf.DB_TYPE == 'postgres':
            cmd = 'PGPASSWORD={} pg_dump -U {} -F c -d {} > {}'.format(dconf.DB_PASSWORD,
                                                                       dconf.DB_USER,
                                                                       dconf.DB_NAME,
                                                                       db_file_path)
        else:
            raise Exception("Database Type {} Not Implemented !".format(dconf.DB_TYPE))
        local(cmd)
        return True


@task
def restore_database():
    if dconf.DB_TYPE == 'oracle':
        # You must create a directory named dpdata through sqlplus in your Oracle database
        # The following script assumes such directory exists.
        # You may want to modify the username, password, and dump file name in the script
        cmd = 'sh oracleScripts/restoreOracle.sh'
    elif dconf.DB_TYPE == 'postgres':
        db_file_path = '{}/{}.dump'.format(dconf.DB_DUMP_DIR, dconf.DB_NAME)
        drop_database()
        create_database()
        cmd = 'PGPASSWORD={} pg_restore -U {} -n public -j 8 -F c -d {} {}'.\
              format(dconf.DB_PASSWORD, dconf.DB_USER, dconf.DB_NAME, db_file_path)
    else:
        raise Exception("Database Type {} Not Implemented !".format(dconf.DB_TYPE))
    LOG.info('Start restoring database')
    local(cmd)
    LOG.info('Finish restoring database')


def _ready_to_start_oltpbench():
    ready = False
    if os.path.exists(dconf.CONTROLLER_LOG):
        with open(dconf.CONTROLLER_LOG, 'r') as f:
            content = f.read()
        ready = 'Output the process pid to' in content
    return ready


def _ready_to_start_controller():
    ready = False
    if os.path.exists(dconf.OLTPBENCH_LOG):
        with open(dconf.OLTPBENCH_LOG, 'r') as f:
            content = f.read()
        ready = 'Warmup complete, starting measurements' in content
    return ready


def _ready_to_shut_down_controller():
    pidfile = os.path.join(dconf.CONTROLLER_HOME, 'pid.txt')
    ready = False
    if os.path.exists(pidfile) and os.path.exists(dconf.OLTPBENCH_LOG):
        with open(dconf.OLTPBENCH_LOG, 'r') as f:
            content = f.read()
        ready = 'Output throughput samples into file' in content
    return ready


def clean_logs():
    # remove oltpbench log
    cmd = 'rm -f {}'.format(dconf.OLTPBENCH_LOG)
    local(cmd)

    # remove controller log
    cmd = 'rm -f {}'.format(dconf.CONTROLLER_LOG)
    local(cmd)


@task
def loop(i):
    # free cache
    free_cache()

    # remove oltpbench log and controller log
    clean_logs()

    # restart database
    restart_database()
    time.sleep(dconf.RESTART_SLEEP_SEC)

    # check disk usage
    if check_disk_usage() > dconf.MAX_DISK_USAGE:
        LOG.warning('Exceeds max disk usage %s', dconf.MAX_DISK_USAGE)

    # run controller from another process
    p = Process(target=run_controller, args=())
    p.start()
    LOG.info('Run the controller')

    # run oltpbench as a background job
    while not _ready_to_start_oltpbench():
        time.sleep(1)
    run_oltpbench_bg()
    LOG.info('Run OLTP-Bench')

    # the controller starts the first collection
    while not _ready_to_start_controller():
        time.sleep(1)
    signal_controller()
    LOG.info('Start the first collection')

    # stop the experiment
    while not _ready_to_shut_down_controller():
        time.sleep(1)

    signal_controller()
    LOG.info('Start the second collection, shut down the controller')

    p.join()

    # add user defined target objective
    # add_udf()

    # save result
    result_timestamp = save_dbms_result()

    if i >= dconf.WARMUP_ITERATIONS:
        # upload result
        upload_result()

        # get result
        response = get_result()

        # save next config
        save_next_config(response, t=result_timestamp)

        # change config
        change_conf(response['recommendation'])


@task
def run_loops(max_iter=1):
    # dump database if it's not done before.
    dump = dump_database()

    for i in range(int(max_iter)):
        if dconf.RELOAD_INTERVAL > 0:
            if i % dconf.RELOAD_INTERVAL == 0:
                if i == 0 and dump is False:
                    restart_database()
                    restore_database()
                elif i > 0:
                    restore_database()

        LOG.info('The %s-th Loop Starts / Total Loops %s', i + 1, max_iter)
        loop(i % dconf.RELOAD_INTERVAL)
        LOG.info('The %s-th Loop Ends / Total Loops %s', i + 1, max_iter)


@task
def rename_batch(result_dir=None):
    result_dir = result_dir or dconf.RESULT_DIR
    results = glob.glob(os.path.join(result_dir, '*__summary.json'))
    results = sorted(results)
    for i, result in enumerate(results):
        prefix = os.path.basename(result)
        prefix_len = os.path.basename(result).find('_') + 2
        prefix = prefix[:prefix_len]
        new_prefix = str(i) + '__'
        for base in ('summary', 'knobs', 'metrics_before', 'metrics_after'):
            fpath = os.path.join(result_dir, prefix + base + '.json')
            rename_path = os.path.join(result_dir, new_prefix + base + '.json')
            os.rename(fpath, rename_path)


def _http_content_to_json(content):
    if isinstance(content, bytes):
        content = content.decode('utf-8')
    try:
        json_content = json.loads(content)
        decoded = True
    except (TypeError, json.decoder.JSONDecodeError):
        json_content = None
        decoded = False

    return json_content, decoded


def _modify_website_object(obj_name, action, verbose=False, **kwargs):
    verbose = _parse_bool(verbose)
    if obj_name == 'project':
        valid_actions = ('create', 'edit')
    elif obj_name == 'session':
        valid_actions = ('create', 'edit')
    elif obj_name == 'user':
        valid_actions = ('create', 'delete')
    else:
        raise ValueError('Invalid object: {}. Valid objects: project, session'.format(obj_name))

    if action not in valid_actions:
        raise ValueError('Invalid action: {}. Valid actions: {}'.format(
            action, ', '.join(valid_actions)))

    data = {}
    for k, v in kwargs.items():
        if isinstance(v, (dict, list, tuple)):
            v = json.dumps(v)
        data[k] = v

    url_path = '/{}/{}/'.format(action, obj_name)
    response = requests.post(CONF['upload_url'] + url_path, data=data)

    content = response.content.decode('utf-8')
    if response.status_code != 200:
        raise Exception("Failed to {} new {}.\nStatus: {}\nMessage: {}\n".format(
            action, obj_name, response.status_code, content))

    json_content, decoded = _http_content_to_json(content)
    if verbose:
        if decoded:
            LOG.info('\n%s_%s = %s', action.upper(), obj_name.upper(),
                     json.dumps(json_content, indent=4))
        else:
            LOG.warning("Content could not be decoded.\n\n%s\n", content)

    return response, json_content, decoded


@task
def create_website_user(**kwargs):
    return _modify_website_object('user', 'create', **kwargs)


@task
def delete_website_user(**kwargs):
    return _modify_website_object('user', 'delete', **kwargs)


@task
def create_website_project(**kwargs):
    return _modify_website_object('project', 'create', **kwargs)


@task
def edit_website_project(**kwargs):
    return _modify_website_object('project', 'edit', **kwargs)


@task
def create_website_session(**kwargs):
    return _modify_website_object('session', 'create', **kwargs)


@task
def edit_website_session(**kwargs):
    return _modify_website_object('session', 'edit', **kwargs)


def wait_pipeline_data_ready(max_time_sec=800, interval_sec=10):
    max_time_sec = int(max_time_sec)
    interval_sec = int(interval_sec)
    elapsed = 0

    while elapsed <= max_time_sec:
        response = requests.get(dconf.WEBSITE_URL + '/test/pipeline/')
        response = response.content
        LOG.info(response)
        if 'False' in str(response):
            time.sleep(interval_sec)
            elapsed += interval_sec
        else:
            return


@task
def integration_tests():

    # Create test website
    response = requests.get(dconf.WEBSITE_URL + '/test/create/')
    LOG.info(response.content)

    # Upload training data
    LOG.info('Upload training data to no tuning session')
    upload_batch(result_dir='../../integrationTests/data/', upload_code='ottertuneTestNoTuning')

    # wait celery periodic task finishes
    wait_pipeline_data_ready()

    # Test DNN
    LOG.info('Test DNN (deep neural network)')
    upload_result(result_dir='../../integrationTests/data/', prefix='0__',
                  upload_code='ottertuneTestTuningDNN')
    response = get_result(upload_code='ottertuneTestTuningDNN')
    assert response['status'] == 'good'

    # Test GPR
    LOG.info('Test GPR (gaussian process regression)')
    upload_result(result_dir='../../integrationTests/data/', prefix='0__',
                  upload_code='ottertuneTestTuningGPR')
    response = get_result(upload_code='ottertuneTestTuningGPR')
    assert response['status'] == 'good'
