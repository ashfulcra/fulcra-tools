// chrome/src/safari/protocol.ts
//
// The content-script -> background message name, in one place so the two
// entry points cannot drift. A mistyped string here is a capture pipeline that
// runs, logs nothing, and delivers nothing.

export const SAFARI_EVENT_MESSAGE = "fulcra.attention.visit";

/** Popup -> background: is the native app holding a token? */
export const SAFARI_AUTH_QUERY = "fulcra.attention.authState";
