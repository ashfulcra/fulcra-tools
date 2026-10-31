"""Entirely synthetic observed-reader Journal fixtures."""
import json
from uuid import UUID
from fulcra_collect.migration_plan import MigrationPlan


def uid(n):
    return str(UUID(int=n))


def plan(**changes):
    from fulcra_dayone.v1 import UNIFIED_JOURNAL_SPEC
    schema = json.loads(UNIFIED_JOURNAL_SPEC.create_body()['record_spec']['schema'])
    schema['properties'].update({key: {} for key in ('id', 'start_time', 'end_time', 'sources', 'tags')})
    schema['additionalProperties'] = False
    return MigrationPlan(dict(version=1, plan_id=uid(1), account_id=uid(2),
        source_api_version='v1alpha1', source_data_type='MomentAnnotation/' + uid(3),
        destination_api_version='v1', destination_data_type='Event/' + uid(4),
        converter='collect.journal.reader.v1', identity_version=1,
        source_prefixes=['com.fulcra.dayone.'], start_time='2020-01-01T00:00:00Z',
        end_time='2021-01-01T00:00:00Z', destination_schema=schema) | changes)


def raw(**changes):
    binding = 'com.fulcradynamics.annotation.' + uid(3)
    return dict(id=uid(5), recorded_at='2020-11-01T01:30:00-04:00',
        source_name='Original display', source_id=binding, metadata=dict(id=uid(3),
            fulcra_userid=uid(2), fulcra_source_id=binding, annotation_type='moment',
            name='Different definition name'), note='Opaque café 日本語 🐈\n# No title derivation',
        sources=[binding, 'com.fulcra.dayone.0123456789abcdef'], tags=[uid(6)]) | changes
