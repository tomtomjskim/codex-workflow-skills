const RUN: Record<string, string> = {
  start: "Not started",
  write_manifest: "Writing files in private staging",
  write_inventory: "Writing files in private staging",
  fsync: "Staged, ready to publish",
  publish: "Staged, ready to publish",
  check: "Staged, ready to publish",
  plain_rename: "Saw the path free, renaming",
  late_manifest: "Published, files still missing",
  late_inventory: "Published, files still missing",
  cleanup: "Refused, removing staging",
  done: "Reported success",
  failed: "Reported failure, staging removed",
  crashed: "Killed, staging left behind",
};

const PATH = (t: any): string => {
  if (t.kind === "none") return "Free";
  if (t.owner === "external") {
    if (t.kind === "dir") return t.files.length ? "Someone else's folder" : "Someone else's empty folder";
    return t.kind === "file" ? "Someone else's file" : "Someone else's symlink";
  }
  return "This run's output";
};

export const title = "prepare-coordination publishing one --out-dir";

export const state = (v: any) => ({ Run: RUN[v.pc.p1], "Output path": PATH(v.target) });

export const group = (v: any): string => {
  const pc = v.pc.p1;
  if (pc === "done") return "success";
  if (pc === "failed") return "refused";
  if (pc === "crashed") return "crashed";
  return "working";
};

export const colors = { working: "blue", success: "teal", refused: "yellow", crashed: "pink" };

export const step = (action: string, _from: any, to: any): string | null => {
  switch (action) {
    case "ExternalCreate": {
      const p = PATH(to.target);
      const what = p.replace("Someone else's ", "");
      return "another process creates " + (/^[aeiou]/.test(what) ? "an " : "a ") + what;
    }
    case "Mkdtemp": return "the run makes its private staging folder";
    case "WriteStaged": return "the run writes a file into staging";
    case "WriteFails": return "a write fails";
    case "FsyncDir": return "the run flushes staging to disk";
    case "PublishNoReplace":
      return to.pc.p1 === "cleanup" ? "the no-replace rename is refused" : "the no-replace rename succeeds";
    case "Check": return to.pc.p1 === "cleanup" ? "the run sees the path taken" : "the run sees the path free";
    case "PlainRename": return to.pc.p1 === "cleanup" ? "the plain rename fails" : "the plain rename succeeds";
    case "Cleanup": return "the run removes staging and reports failure";
    case "Crash": return "the process is killed";
    case "Done": return null;
    default: return action;
  }
};
