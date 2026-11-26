// The panel reads session metadata only. It doesn't read prompt text, tool
// arguments, or tool results, and it never invents invocation times.
export const MAX_EVENTS = 200
const MAX_MESSAGES = 200
const MAX_PARTS = 128

function validTime(value) {
  return Number.isSafeInteger(value) && value >= 0 && value <= 8_640_000_000_000_000
}

export function activityEvents(messages) {
  const events = []
  function add(time, label) {
    if (validTime(time)) events.push({ time, label })
  }
  for (const message of messages.slice(-MAX_MESSAGES)) {
    if (message?.type === "user") {
      add(message.time?.created, "Prompt created")
    } else if (message?.type === "assistant") {
      add(message.time?.created, "Assistant response created")
      if (!Array.isArray(message.content)) continue
      for (const part of message.content.slice(-MAX_PARTS)) {
        if (part?.type !== "tool") continue
        const name = typeof part.name === "string" && /^[A-Za-z0-9_.:-]{1,72}$/.test(part.name)
          ? part.name
          : "Tool"
        if (validTime(part.time?.ran)) add(part.time.ran, `${name} invoked`)
        else add(part.time?.created, `${name} call created (invocation time unknown)`)
      }
    }
  }
  return events.reverse().sort((left, right) => right.time - left.time).slice(0, MAX_EVENTS)
}

export function localTimestamp(time) {
  const date = new Date(time)
  const pad = (value) => String(value).padStart(2, "0")
  const offset = -date.getTimezoneOffset()
  const abs = Math.abs(offset)
  const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  const zone = `${offset < 0 ? "-" : "+"}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
  return `${day} ${clock} ${zone}`
}
