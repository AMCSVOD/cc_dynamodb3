import copy
import os

from boto import dynamodb2
from boto.dynamodb2 import fields  # AllIndex, GlobalAllIndex, HashKey, RangeKey
from boto.dynamodb2 import table
from boto.dynamodb2 import types
from boto.exception import JSONResponseError
from bunch import Bunch
import yaml

from .config import Config
from .logging import create_logger


config = Config()
logger = create_logger(name='cc_dynamodb', **config['LOGGING'])

# Cache to avoid parsing YAML file repeatedly.
_cached_config = None

def set_config(**kwargs):
    global _cached_config

    config_path = kwargs.pop('table_config', config['CONFIG_PATH'])
    with open(config_path) as config_file:
        yaml_config = yaml.load(config_file)

    _cached_config = Bunch({
        'yaml': yaml_config,
        'namespace': kwargs.get('namespace')
                        or os.environ.get('CC_DYNAMODB_NAMESPACE'),
        'aws_access_key_id': kwargs.get('aws_access_key_id')
                                or os.environ.get('CC_DYNAMODB_ACCESS_KEY_ID'),
        'aws_secret_access_key': kwargs.get('aws_secret_access_key')
                                    or os.environ.get('CC_DYNAMODB_SECRET_ACCESS_KEY'),
        'host': kwargs.get('host')
                    or os.environ.get('CC_DYNAMODB_HOST'),
        'port': kwargs.get('port')
                    or os.environ.get('CC_DYNAMODB_PORT'),
        'is_secure': kwargs.get('is_secure')
                         or os.environ.get('CC_DYNAMODB_IS_SECURE'),
    })


    if not _cached_config.namespace:
        raise ConfigurationError('Missing namespace kwarg OR environment variable CC_DYNAMODB_NAMESPACE')
    if not _cached_config.aws_access_key_id:
        raise ConfigurationError('Missing aws_access_key_id kwarg OR environment variable CC_DYNAMODB_ACCESS_KEY_ID')
    if not _cached_config.aws_secret_access_key:
        raise ConfigurationError('Missing aws_secret_access_key kwarg OR environment variable CC_DYNAMODB_SECRET_ACCESS_KEY')
    if _cached_config.port:
        try:
            _cached_config.port = int(_cached_config.port)
        except ValueError:
            raise ConfigurationError(
                'Integer value expected for port '
                'OR environment variable CC_DYNAMODB_PORT. Got %s' % _cached_config.port)

    logger.event('cc_dynamodb.set_config', status='config loaded')


def get_config(**kwargs):
    global _cached_config

    if not _cached_config:
        set_config(**kwargs)

    return Bunch(copy.deepcopy(_cached_config.toDict()))


class ConfigurationError(Exception):
    pass


def _build_key(key_details):
    key_details = key_details.copy()
    key_type = getattr(fields, key_details.pop('type'))
    key_details['data_type'] = getattr(types, key_details['data_type'])
    return key_type(**key_details)


def _build_keys(keys_config):
    return [_build_key(key_details)
            for key_details in keys_config]


def _build_secondary_index(index_details):
    index_details = index_details.copy()
    index_type = getattr(fields, index_details.pop('type'))
    parts = []
    for key_details in index_details.get('parts', []):
        parts.append(_build_key(key_details))
    return index_type(index_details['name'], parts=parts)


def _build_secondary_indexes(indexes_config):
    return [_build_secondary_index(index_details)
            for index_details in indexes_config]


def _get_table_metadata(table_name):
    config = get_config().yaml

    try:
        keys_config = config['schemas'][table_name]
    except KeyError:
        logger.exception('cc_dynamodb.UnknownTable', extra=dict(table_name=table_name, config=config))
        raise UnknownTableException('Unknown table: %s' % table_name)

    schema = _build_keys(keys_config)

    global_indexes_config = config.get('global_indexes', {}).get(table_name, [])
    indexes_config = config.get('indexes', {}).get(table_name, [])

    return dict(
        schema=schema,
        global_indexes=_build_secondary_indexes(global_indexes_config),
        indexes=_build_secondary_indexes(indexes_config),
    )


def get_table_name(table_name):
    '''Prefixes the table name for the different environments/settings.'''
    return get_config().namespace + table_name


def get_reverse_table_name(table_name):
    '''Prefixes the table name for the different environments/settings.'''
    prefix_length = len(get_config().namespace)
    return table_name[prefix_length:]


def get_table_index(table_name, index_name):
    """Given a table name and an index name, return the index."""
    import cc_dynamodb
    config = cc_dynamodb.get_config()
    all_indexes = (config.yaml.get('indexes', {}).items() +
                   config.yaml.get('global_indexes', {}).items())
    for config_table_name, table_indexes in all_indexes:
        if config_table_name == table_name:
            for index in table_indexes:
                if index['name'] == index_name:
                    return index


def get_connection():
    """Returns a DynamoDBConnection even if credentials are invalid."""
    config = get_config()

    if config.host:
        from boto.dynamodb2.layer1 import DynamoDBConnection
        return DynamoDBConnection(
            aws_access_key_id=config.aws_access_key_id,
            aws_secret_access_key=config.aws_secret_access_key,
            host=config.host,                           # Host where DynamoDB Local resides
            port=config.port,                           # DynamoDB Local port (8000 is the default)
            is_secure=config.is_secure or False)        # For DynamoDB Local, disable secure connections

    return dynamodb2.connect_to_region(
        'us-west-2',
        aws_access_key_id=config.aws_access_key_id,
        aws_secret_access_key=config.aws_secret_access_key,
    )


def get_table_columns(table_name):
    """Return known columns for a table and their data type."""
    # TODO: see if table.describe() can return what dynamodb knows instead.
    config = get_config().yaml
    try:
        return dict(
            (column_name, getattr(types, column_type))
                for column_name, column_type in config['columns'][table_name].items())
    except KeyError:
        logger.exception('UnknownTable: %s' % table_name, extra={'config': config})
        raise UnknownTableException('Unknown table: %s' % table_name)


def get_table(table_name, connection=None):
    '''Returns a dict with table and preloaded schema, plus columns.

    WARNING: Does not check the table actually exists. Querying against
             a non-existent table raises boto.exception.JSONResponseError

    This function avoids additional lookups when using a table.
    The columns included are only the optional columns you may find in some of the items.
    '''
    return table.Table(
        get_table_name(table_name),
        connection=connection or get_connection(),
        **_get_table_metadata(table_name)
    )


def list_table_names():
    """List known table names from configuration, without namespace."""
    return get_config().yaml['schemas'].keys()


def _get_or_default_throughput(throughput):
    if throughput == False:
    	config = get_config()
        throughput = config.yaml['default_throughput']
    return throughput


def _get_table_init_data(table_name, connection, throughput):
    init_data = dict(
        table_name=get_table_name(table_name),
        connection=connection or get_connection(),
        throughput=_get_or_default_throughput(throughput),
    )
    init_data.update(_get_table_metadata(table_name))
    return init_data


def create_table(table_name, connection=None, throughput=False):
    """Create table. Throws an error if table already exists."""
    try:
        return table.Table.create(**_get_table_init_data(table_name, connection=connection, throughput=throughput))
    except JSONResponseError as e:
        if e.status == 400 and e.error_code == 'ResourceInUseException':
            raise TableAlreadyExistsException(body=e.body)
        raise e


def _validate_schema(schema, table_metadata):
    """Raise error if primary index (schema) is not the same as upstream"""
    upstream_schema = table_metadata['Table']['KeySchema']
    upstream_schema_attributes = [i['AttributeName'] for i in upstream_schema]
    upstream_attributes = [item for item in table_metadata['Table']['AttributeDefinitions']
                           if item['AttributeName'] in upstream_schema_attributes]

    local_schema = [item.schema() for item in schema]
    local_schema_attributes = [i['AttributeName'] for i in local_schema]
    local_attributes = [item.definition() for item in schema
                        if item.definition()['AttributeName'] in local_schema_attributes]

    if sorted(upstream_schema, key=lambda i: i['AttributeName']) != sorted(local_schema, key=lambda i: i['AttributeName']):
        raise UpdateTableException('Mismatched schema: %s VS %s' % (upstream_schema, local_schema))

    if sorted(upstream_attributes, key=lambda i: i['AttributeName']) != sorted(local_attributes, key=lambda i: i['AttributeName']):
        raise UpdateTableException('Mismatched attributes: %s VS %s' % (upstream_attributes, local_attributes))


def update_table(table_name, connection=None, throughput=False):
    """Update existing table."""
    db_table = table.Table(**_get_table_init_data(table_name, connection=connection, throughput=throughput))
    local_global_indexes_by_name = dict((index.name, index) for index in db_table.global_indexes)
    try:
        table_metadata = db_table.describe()
        _validate_schema(schema=db_table.schema, table_metadata=table_metadata)

        # Update existing primary index throughput
        db_table.update(throughput=throughput)

        upstream_global_indexes_by_name = dict((index['IndexName'], index)
                                               for index in table_metadata['Table'].get('GlobalSecondaryIndexes', []))
        for index_name, index in local_global_indexes_by_name.items():
            if index_name not in upstream_global_indexes_by_name:
                db_table.create_global_secondary_index(index)
                logger.info('Creating GSI %s for %s' % (index_name, table_name))
            else:
                # Update throughput
                # TODO: this could be done in a single call with multiple indexes
                db_table.update_global_secondary_index(global_indexes={
                    index_name: index.throughput
                })
                logger.info('Updating GSI %s throughput for %s to %s' % (index_name, table_name, index.throughput))

        for index_name in upstream_global_indexes_by_name.keys():
            if index_name not in local_global_indexes_by_name:
                db_table.delete_global_secondary_index(index_name)
                logger.info('Deleting GSI %s for %s' % (index_name, table_name))

    except JSONResponseError as e:
        if e.status == 400 and e.error_code == 'ResourceNotFoundException':
            raise UnknownTableException('Unknown table: %s' % table_name)

    return db_table


class UnknownTableException(Exception):
    pass


class TableAlreadyExistsException(Exception):
    def __init__(self, body):
        self.body = body


class UpdateTableException(Exception):
    pass