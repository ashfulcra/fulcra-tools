export type WorkState =
  | 'waiting'
  | 'working'
  | 'decision-needed'
  | 'prepared-for-approval'
  | 'result-ready'
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
  /** The sending agent's name where the format carries one (v1, workspace); null for legacy mesh. */
  sender?: string | null
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

export type DraftOption = { label: string; body: string }

export type Invite = {
  channel: string
  name: string
  message: string
  text: string
  createdAt: string
  revoked: boolean
}

export type Draft = {
  /** A file share held for approval: nothing is uploaded or granted until approved. */
  sharePath?: string
  /** Concrete choices for a decision ("Book Monday at 10" / "Keep Friday"); each sends its own body. */
  options: DraftOption[]
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

export type PaneView = { kind: 'home' } | { kind: 'settings' } | { kind: 'invite' } | { kind: 'collab'; key: string } | { kind: 'contact'; key: string }

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
      overrides: Record<string, string>
      invites: Invite[]
      expanded: string[]
      aliases: Record<string, string>
      hidden: Record<string, string>
      adopted: string[]
      owned: Record<string, string>
    }
  }
}
