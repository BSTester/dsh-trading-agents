"""指令目录（规格 §8.2）：工作台 → daemon 的唯一通道。白名单外一律拒绝；
nonce + processed/ 目录保证幂等；原子写防半文件。行为在 WP4 任务 2 补齐。"""

COMMANDS = {"execute_plan", "cancel_plan", "kill", "unkill", "run_job"}
