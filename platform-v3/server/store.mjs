// 轻量持久化：JSONL 追加（headless 调用日志 / 决策链）+ JSON 快照（审批/设置）。
// FR-MON-003：headless 调用日志写平台库，不依赖 Harness 的 Session 存储。
import fs from 'node:fs'
import path from 'node:path'

export function createStore(dir) {
  fs.mkdirSync(dir, { recursive: true })
  const file = (name) => path.join(dir, name)

  return {
    dir,
    append(name, record) {
      fs.appendFileSync(file(name), JSON.stringify(record) + '\n')
    },
    readAll(name) {
      try {
        return fs
          .readFileSync(file(name), 'utf8')
          .split('\n')
          .filter((line) => line.trim() !== '')
          .map((line) => {
            try {
              return JSON.parse(line)
            } catch {
              return null
            }
          })
          .filter(Boolean)
      } catch {
        return []
      }
    },
    writeJson(name, obj) {
      fs.writeFileSync(file(name), JSON.stringify(obj, null, 1))
    },
    readJson(name) {
      try {
        return JSON.parse(fs.readFileSync(file(name), 'utf8'))
      } catch {
        return null
      }
    },
  }
}

export default createStore
