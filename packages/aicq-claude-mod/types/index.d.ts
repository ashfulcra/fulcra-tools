export type WorkState =
  | 'waiting'
  | 'working'
  | 'decision-needed'
  | 'prepared-for-approval'
  | 'completed'
  | 'paused'
  | 'unable'
  | 'needs-reply'
  | 'fyi'

export type ArtifactRef = {
  /** Fulcra Files path (recipient-readable via share) or URL. */
  uri: string
  name: string
  version: string | null
  sha256: string | null
}

export type AicqMessage = {
  id: string
  source: 'mesh' | 'workspace'
  /** in: from the contact; out: sent by this owner (any of their agents). */
  direction: 'in' | 'out'
  channel: string
  contact: string
  contactUserId: string | null
  workspace: string | null
  to: string
  kind: string
  topic: string
  body: string
  at: string
  inReplyTo: string | null
  /** Explicit work update carried by the message, if any. */
  state: WorkState | null
  purpose: string | null
  artifacts: ArtifactRef[]
}

export type AicqStatus = {
  lastCheckAt: string | null
  checking: boolean
  degraded: string[]
  newSinceLook: number
  contacts: number
}

export type Draft = {
  /** Why it needs the owner (consequential decision), if the agent said. */
  question: string | null
  state: WorkState | null
  id: string
  collabKey: string
  to: string
  toUser: string | null
  workspace: string | null
  topic: string
  body: string
  inReplyTo: string | null
  createdAt: string
}

export type AttachedContext = { collabKey: string; title: string; text: string }

export type PaneView = { kind: 'home' } | { kind: 'settings' } | { kind: 'collab'; key: string } | { kind: 'contact'; key: string }

declare module 'claude-code' {
  interface PluginState {
    aicq: {
      inbox: AicqMessage[]
      status: AicqStatus
      view: PaneView
      drafts: Draft[]
      paused: string[]
      attached: AttachedContext | null
      mode: string
      showQuiet: boolean
      query: string
    }
  }
}
