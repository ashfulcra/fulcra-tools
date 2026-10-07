export type AicqMessage = {
  id: string
  source: 'mesh' | 'workspace'
  channel: string
  contact: string
  contactUserId: string | null
  workspace: string | null
  to: string
  kind: string
  topic: string
  body: string
  at: string
}

export type AicqStatus = {
  lastCheckAt: string | null
  checking: boolean
  degraded: string[]
  newSinceLook: number
  contacts: number
}

declare module 'claude-code' {
  interface PluginState {
    aicq: { inbox: AicqMessage[]; status: AicqStatus }
  }
}
