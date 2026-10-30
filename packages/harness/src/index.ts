/**
 * S2 Harness：插件化宿主。
 *
 * 对外只暴露「起宿主、跑 CLI、插件契约、清单」这几样；
 * 具体能力（store / workspace / exec / agent / telemetry / sessions / stages）都是插件，
 * 由 vibe.plugins.yml 组合，不在这个入口里硬编码。
 */

export { runCli, parseArgv } from './cli.ts';
export type { CliOptions } from './cli.ts';
export { loadHost, formatReports, FIBER_STATES } from './host.ts';
export type { Host, PluginReport, LoadOptions } from './host.ts';
export { parseManifest, applyPatch, interpolate, resolveModuleSpecifier } from './manifest.ts';
export type { Manifest, PluginRow } from './manifest.ts';
export * from './kernel.ts';
export { dirDigests, databaseDigest, sha256File, batchIdOf } from './fs-util.ts';
export type { Digest, TableDigest } from './fs-util.ts';
