#!/usr/bin/env node
import { response, toSkillError, SkillError, resultExitCode } from '../src/protocol.mjs';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { isGuangyapanLaunchdLabel } from '../src/platform.mjs';
import { assertRuntimeReady, parseAgentOptions, loadConfig, assertSupportedPlatform } from '../src/config.mjs';

const [command, ...args] = process.argv.slice(2);
const CLI_NAME = 'guangyapan';

try { assertSupportedPlatform(); } catch (error) {
  process.stdout.write(`${JSON.stringify(response(command || 'unknown', {
    code: error.code, data: { message: error.message },
  }))}\n`);
  process.exit(1);
}

if (isHelpRequest(command, args)) {
  process.stdout.write(helpText());
} else {
await assertRuntimeReady().catch((error) => {
  const failure = toSkillError(error);
  process.stdout.write(`${JSON.stringify(response(command || 'unknown', {
    code: failure.code,
    retryable: false,
    data: { message: failure.message, ...failure.data },
  }))}\n`);
  process.exit(1);
});

const {
  createDependencies,
  reloginCommand,
  logoutCommand,
  runCommand,
  statusCommand,
  stopCommand,
} = await import('../src/commands.mjs');
const { loginCommand, loginStatusCommand, refreshLoginCommand, runLoginWorker } = await import('../src/login.mjs');
const { runWorker } = await import('../src/worker.mjs');
const { runWorkerSupervisor } = await import('../src/worker.mjs');

if (command === '__worker') {
  const batchId = flag(args, '--batch-id');
  const planDigest = flag(args, '--plan-digest');
  const workerInstanceId = flag(args, '--instance-id');
  if (!batchId || !planDigest || !workerInstanceId) process.exit(2);
  try {
    await runWorker(batchId, {
      expectedPlanDigest: planDigest,
      workerInstanceId,
    });
  } finally {
    scheduleLaunchdCleanup();
  }
  process.exit(0);
}

if (command === '__worker-supervisor') {
  const batchId = flag(args, '--batch-id');
  const planDigest = flag(args, '--plan-digest');
  const workerInstanceId = flag(args, '--instance-id');
  if (!batchId || !planDigest || !workerInstanceId) process.exit(2);
  try {
    await runWorkerSupervisor({
      batchId,
      planDigest,
      instanceId: workerInstanceId,
      bin: fileURLToPath(import.meta.url),
    });
  } finally {
    scheduleLaunchdCleanup();
  }
  process.exit(0);
}

if (command === '__login-worker') {
  const workerInstanceId = flag(args, '--instance-id');
  if (!workerInstanceId) process.exit(2);
  try {
    await runLoginWorker({ workerInstanceId });
  } finally {
    scheduleLaunchdCleanup();
  }
  process.exit(0);
}

if (command === '__background-launcher') {
  const [target, ...targetArgs] = args;
  if (!['__worker', '__worker-supervisor', '__login-worker'].includes(target)) process.exit(2);
  const bin = fileURLToPath(import.meta.url);
  const child = spawn(process.execPath, [bin, target, ...targetArgs], {
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
  });
  child.unref();
  process.exit(0);
}

let dependencies;
let result;
try {
  const invocation = parseAgentOptions(args);
  args.splice(0, args.length, ...invocation.args);
  dependencies = createDependencies({ config: loadConfig(invocation.context) });
  switch (command) {
    case 'login':
      assertOptions(args, new Set());
      result = await loginCommand(dependencies);
      break;
    case 'login-status':
      assertOptions(args, new Set());
      result = await loginStatusCommand(dependencies);
      break;
    case 'relogin':
      assertOptions(args, new Set());
      result = await reloginCommand(dependencies);
      break;
    case 'refresh-login':
      assertOptions(args, new Set());
      result = await refreshLoginCommand(dependencies);
      break;
    case 'run':
      result = await runCommand(await readRunInput(args), dependencies);
      break;
    case 'status':
      assertOptions(args, new Set(['--batch-id']));
      result = await statusCommand(requiredFlag(args, '--batch-id'), dependencies);
      break;
    case 'stop':
      assertOptions(args, new Set(['--batch-id']));
      result = await stopCommand(requiredFlag(args, '--batch-id'), dependencies);
      break;
    case 'logout':
      assertOptions(args, new Set());
      result = await logoutCommand(dependencies);
      break;
    default:
      throw new SkillError('INVALID_REQUEST', `用法: ${CLI_NAME} login | login-status | relogin | refresh-login | run [JSON] | status --batch-id ID | stop --batch-id ID | logout`);
  }
  process.stdout.write(`${JSON.stringify(result)}\n`);
  process.exitCode = resultExitCode(result);
} catch (error) {
  const failure = toSkillError(error);
  process.stdout.write(`${JSON.stringify(response(command || 'unknown', {
    code: failure.code,
    retryable: failure.retryable,
    data: { message: failure.message, ...failure.data },
  }))}\n`);
  process.exitCode = 1;
} finally {
  // Cleanup must not change the command result or remove the batch just inspected.
  await dependencies?.store.cleanupExpired({ excludeBatchIds: [result?.batch_id].filter(Boolean) }).catch(() => {});
}
}

function isHelpRequest(command, args) {
  return ['help', '--help', '-h'].includes(command) ||
    args.some((value) => value === '--help' || value === '-h');
}

function helpText() {
  return [
    '光鸭网盘 CLI',
    '',
    `用法: ${CLI_NAME} <命令> [参数]`,
    '',
    '命令:',
    '  login                         登录',
    '  login-status                  查询登录状态',
    '  relogin                       重新登录或切换账号',
    '  refresh-login                 刷新登录态',
    '  run [JSON]                    执行 V1 请求，也可从 stdin 读取',
    '  status --batch-id ID          查询后台任务',
    '  stop --batch-id ID            停止后台任务',
    '  logout                         退出登录',
    '',
    '选项:',
    '  --agent-name NAME             当前调用的 Agent 产品名称',
    '  --agent-profile-dir PATH     当前 Agent 专属配置目录的绝对路径',
    '  --agent-instance-id ID        当前 Agent 实例的固定 UUIDv4/v7',
    '                                配置目录与实例标识二选一，所有命令保持一致',
    '  -h, --help                    显示帮助',
    '',
  ].join('\n');
}

async function readRunInput(args) {
  if (args.length > 1) throw new SkillError('INVALID_REQUEST', 'run 只接受一个 JSON 参数或 stdin');
  if (args.length === 1) return args[0];
  const chunks = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > 1024 * 1024) throw new SkillError('INVALID_REQUEST', '请求超过 1 MiB');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

function flag(args, name) {
  const index = args.indexOf(name);
  if (index < 0) return undefined;
  if (index + 1 >= args.length || args[index + 1].startsWith('--')) {
    throw new SkillError('INVALID_REQUEST', `${name} 缺少值`);
  }
  if (args.indexOf(name, index + 1) >= 0) throw new SkillError('INVALID_REQUEST', `${name} 重复`);
  return args[index + 1];
}

function requiredFlag(args, name) {
  const value = flag(args, name);
  if (!value) throw new SkillError('INVALID_REQUEST', `${name} 必传`);
  return value;
}

function assertOptions(args, allowed) {
  if (args.length % 2 !== 0) {
    throw new SkillError('INVALID_REQUEST', '命令参数必须使用 --name value');
  }
  for (let index = 0; index < args.length; index += 2) {
    if (!allowed.has(args[index]) || args[index + 1]?.startsWith('--')) {
      throw new SkillError('INVALID_REQUEST', `不支持的参数: ${args[index]}`);
    }
  }
}

function scheduleLaunchdCleanup() {
  const label = process.env.GUANGYAPAN_DRIVE_LAUNCHD_LABEL;
  if (process.env.GUANGYAPAN_DRIVE_NO_LAUNCHD_CLEANUP === '1' ||
      process.platform !== 'darwin' || !isGuangyapanLaunchdLabel(label)) return;
  const child = spawn('launchctl', ['remove', label], {
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
  });
  child.on('error', () => {});
  child.unref();
}
