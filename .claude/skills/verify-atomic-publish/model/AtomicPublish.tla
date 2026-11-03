---------------------------- MODULE AtomicPublish ----------------------------
(***************************************************************************)
(* _publish_prepared_output in scripts/workflow, with several concurrent   *)
(* prepare-coordination runs aiming at one --out-dir and one outside actor *)
(* that may create that path at any moment.                                *)
(***************************************************************************)
EXTENDS FiniteSets

CONSTANTS Preparers, Variant

Files == {"manifest", "inventory"}
NoOwner == "nobody"
NoTarget == [kind |-> "none", owner |-> NoOwner, files |-> {}]
ExternalShapes ==
    { [kind |-> "dir",     owner |-> "external", files |-> {}],
      [kind |-> "dir",     owner |-> "external", files |-> {"marker"}],
      [kind |-> "file",    owner |-> "external", files |-> {}],
      [kind |-> "symlink", owner |-> "external", files |-> {}] }

VARIABLES target, stage, pc, first
vars == <<target, stage, pc, first>>

TypeOK ==
    /\ target.kind \in {"none", "dir", "file", "symlink"}
    /\ target.owner \in Preparers \cup {"external", NoOwner}
    /\ target.files \subseteq Files \cup {"marker"}
    /\ \A p \in Preparers : stage[p].exists \in BOOLEAN /\ stage[p].files \subseteq Files

Init ==
    /\ target = NoTarget
    /\ stage = [p \in Preparers |-> [exists |-> FALSE, files |-> {}]]
    /\ pc = [p \in Preparers |-> "start"]
    /\ first = NoTarget

SetTarget(t) ==
    /\ target' = t
    /\ first' = IF first.kind = "none" THEN t ELSE first

DropStage(p) == stage' = [stage EXCEPT ![p] = [exists |-> FALSE, files |-> {}]]
Goto(p, s) == pc' = [pc EXCEPT ![p] = s]

\* Someone else mkdir/touch/ln -s the output path.
ExternalCreate ==
    /\ target.kind = "none"
    /\ \E t \in ExternalShapes : SetTarget(t)
    /\ UNCHANGED <<stage, pc>>

\* tempfile.mkdtemp(prefix=".<name>.", dir=parent): a fresh private sibling.
Mkdtemp(p) ==
    /\ pc[p] = "start"
    /\ stage' = [stage EXCEPT ![p] = [exists |-> TRUE, files |-> {}]]
    /\ Goto(p, IF Variant = "publish_then_write" THEN "publish" ELSE "write_manifest")
    /\ UNCHANGED <<target, first>>

\* _write_staged_file(staged / "manifest.json") and "inventory.json".
WriteStaged(p) ==
    \/ /\ pc[p] = "write_manifest"
       /\ stage' = [stage EXCEPT ![p].files = @ \cup {"manifest"}]
       /\ Goto(p, "write_inventory")
       /\ UNCHANGED <<target, first>>
    \/ /\ pc[p] = "write_inventory"
       /\ stage' = [stage EXCEPT ![p].files = @ \cup {"inventory"}]
       /\ Goto(p, "fsync")
       /\ UNCHANGED <<target, first>>

\* A write raises (disk full, EEXIST from "xb"): the except branch runs.
WriteFails(p) ==
    /\ pc[p] \in {"write_manifest", "write_inventory"}
    /\ Goto(p, "cleanup")
    /\ UNCHANGED <<target, stage, first>>

\* os.fsync on the staged directory. No visible effect in this model.
FsyncDir(p) ==
    /\ pc[p] = "fsync"
    /\ Goto(p, IF Variant = "check_then_rename" THEN "check" ELSE "publish")
    /\ UNCHANGED <<target, stage, first>>

\* renamex_np(RENAME_EXCL) / renameat2(RENAME_NOREPLACE): one atomic step
\* that succeeds only when the path does not exist.
PublishNoReplace(p) ==
    /\ pc[p] = "publish"
    /\ IF target.kind = "none"
         THEN /\ SetTarget([kind |-> "dir", owner |-> p, files |-> stage[p].files])
              /\ DropStage(p)
              /\ Goto(p, IF Variant = "publish_then_write" THEN "late_manifest" ELSE "done")
         ELSE /\ Goto(p, "cleanup")
              /\ UNCHANGED <<target, stage, first>>

\* Broken variant check_then_rename: exists() then plain rename(2), which
\* replaces an empty directory and fails on anything else.
Check(p) ==
    /\ pc[p] = "check"
    /\ Goto(p, IF target.kind = "none" THEN "plain_rename" ELSE "cleanup")
    /\ UNCHANGED <<target, stage, first>>

PlainRename(p) ==
    /\ pc[p] = "plain_rename"
    /\ IF target.kind = "none" \/ (target.kind = "dir" /\ target.files = {})
         THEN /\ target' = [kind |-> "dir", owner |-> p, files |-> stage[p].files]
              /\ first' = IF first.kind = "none" THEN target' ELSE first
              /\ DropStage(p)
              /\ Goto(p, "done")
         ELSE /\ Goto(p, "cleanup")
              /\ UNCHANGED <<target, stage, first>>

\* Broken variant publish_then_write: files written after the rename.
LateWrite(p) ==
    \/ /\ pc[p] = "late_manifest"
       /\ target' = [target EXCEPT !.files = @ \cup {"manifest"}]
       /\ Goto(p, "late_inventory")
       /\ UNCHANGED <<stage, first>>
    \/ /\ pc[p] = "late_inventory"
       /\ target' = [target EXCEPT !.files = @ \cup {"inventory"}]
       /\ Goto(p, "done")
       /\ UNCHANGED <<stage, first>>

\* except Exception: if staged.exists(): shutil.rmtree(staged); raise
\* Broken variant no_cleanup skips the rmtree.
Cleanup(p) ==
    /\ pc[p] = "cleanup"
    /\ IF Variant = "no_cleanup" THEN UNCHANGED stage ELSE DropStage(p)
    /\ Goto(p, "failed")
    /\ UNCHANGED <<target, first>>

\* kill -9 or power loss between any two steps. The staged sibling stays.
Crash(p) ==
    /\ pc[p] \notin {"start", "done", "failed", "crashed"}
    /\ Goto(p, "crashed")
    /\ UNCHANGED <<target, stage, first>>

Next ==
    \/ ExternalCreate
    \/ \E p \in Preparers :
         \/ Mkdtemp(p) \/ WriteStaged(p) \/ WriteFails(p) \/ FsyncDir(p)
         \/ PublishNoReplace(p) \/ Check(p) \/ PlainRename(p) \/ LateWrite(p)
         \/ Cleanup(p) \/ Crash(p)

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
\* Once the output path exists, nobody replaces it.
NoReplace == first.kind # "none" => /\ target.kind = first.kind
                                    /\ target.owner = first.owner

\* A path someone else created keeps its exact shape and contents.
ExternalPreserved == first.owner = "external" => target = first

\* A published output directory always holds both files.
Complete == target.owner \in Preparers => target.files = Files

\* At most one run reports success, and it owns the output.
SingleWinner == \A p, q \in Preparers : pc[p] = "done" /\ pc[q] = "done" => p = q
WinnerOwns == \A p \in Preparers : pc[p] = "done" => target.owner = p

\* A run that ends normally, success or failure, leaves no staged sibling.
NoLeftover == \A p \in Preparers : pc[p] \in {"done", "failed"} => ~stage[p].exists
=============================================================================
