import { describe, expect, it } from 'vitest';
import { validateWorkTransportConfig } from './work-transport-config.js';

export const configValue = {
  baseUrl: 'https://api.fulcradynamics.com/',
  principalId: '00000000-0000-4000-8000-000000000900',
  channel: 'MomentAnnotation/00000000-0000-4000-8000-000000000901',
  workspaceId: '00000000-0000-4000-8000-000000000100',
  workstreamId: '00000000-0000-4000-8000-000000000300',
  actorBinding: {
    principal_id: '00000000-0000-4000-8000-000000000900',
    logical_agent_id: 'synthetic-agent',
    instance_id: 'synthetic-instance',
    session_id: 'synthetic-session'
  }
};

describe('private synthetic transport configuration', () => {
  it('returns an isolated, deeply frozen declaration with no capability grant', () => {
    const source = structuredClone(configValue);
    const checked = validateWorkTransportConfig(source);
    source.actorBinding.logical_agent_id = 'changed';
    expect(checked).toEqual(configValue);
    expect(Object.isFrozen(checked)).toBe(true);
    expect(Object.isFrozen(checked.actorBinding)).toBe(true);
    expect(checked.actorBinding.logical_agent_id).toBe('synthetic-agent');
  });

  it.each([
    { baseUrl: 'https://other.example/' },
    { principalId: '00000000-0000-4000-8000-000000000001' },
    { channel: 'MomentAnnotation/00000000-0000-4000-8000-000000000001' },
    { workspaceId: 'not-a-uuid' },
    { workstreamId: 'not-a-uuid' },
    { capabilities: ['work.write'] },
    { trust: {} },
    { actorBinding: { ...configValue.actorBinding, principal_id: 'other' } },
    { actorBinding: { ...configValue.actorBinding, logical_agent_id: 'bad id' } },
    { actorBinding: { ...configValue.actorBinding, capability: 'work.write' } }
  ])('rejects unpinned scope, unknown authority, or unsafe actor binding %#', (change) => {
    expect(() => validateWorkTransportConfig({ ...configValue, ...change })).toThrow(
      'INVALID_CONFIG'
    );
  });
});
