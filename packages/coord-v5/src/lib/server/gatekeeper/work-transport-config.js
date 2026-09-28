const BASE_URL = 'https://api.fulcradynamics.com/';
const PRINCIPAL_ID = '00000000-0000-4000-8000-000000000900';
const CHANNEL = 'MomentAnnotation/00000000-0000-4000-8000-000000000901';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$/;
const CONFIG_KEYS = [
  'baseUrl',
  'principalId',
  'channel',
  'workspaceId',
  'workstreamId',
  'actorBinding'
];
const ACTOR_KEYS = ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'];

/** @param {unknown} value */
function plain(value) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}
/** @param {unknown} value @param {string[]} keys */
function exact(value, keys) {
  if (!plain(value)) return false;
  const candidate = /** @type {object} */ (value);
  return (
    Reflect.ownKeys(candidate).length === keys.length &&
    keys.every((key) => {
      const descriptor = Object.getOwnPropertyDescriptor(candidate, key);
      return (
        Object.hasOwn(candidate, key) &&
        descriptor?.enumerable === true &&
        Object.hasOwn(descriptor, 'value')
      );
    })
  );
}

/** Validate only the fixed synthetic transport declaration; this does not grant actor authority.
 * @param {unknown} value */
export function validateWorkTransportConfig(value) {
  if (!exact(value, CONFIG_KEYS)) throw new TypeError('INVALID_CONFIG');
  const config = /** @type {any} */ (value);
  if (
    config.baseUrl !== BASE_URL ||
    config.principalId !== PRINCIPAL_ID ||
    config.channel !== CHANNEL ||
    !UUID.test(config.workspaceId) ||
    !UUID.test(config.workstreamId) ||
    !exact(config.actorBinding, ACTOR_KEYS) ||
    config.actorBinding.principal_id !== PRINCIPAL_ID ||
    !ACTOR_KEYS.every(
      (key) =>
        typeof config.actorBinding[key] === 'string' && IDENTIFIER.test(config.actorBinding[key])
    )
  )
    throw new TypeError('INVALID_CONFIG');
  const actorBinding = Object.freeze(
    Object.fromEntries(ACTOR_KEYS.map((key) => [key, config.actorBinding[key]]))
  );
  return Object.freeze({
    baseUrl: BASE_URL,
    principalId: PRINCIPAL_ID,
    channel: CHANNEL,
    workspaceId: config.workspaceId,
    workstreamId: config.workstreamId,
    actorBinding
  });
}
