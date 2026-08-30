// Command brainite-hook is the capture and injection client for Brainite.
//
// One binary, one subcommand per hook event. Compiled rather than scripted
// because UserPromptSubmit runs on every prompt and PostToolUse on every tool
// call: a Node or Python cold start would tax every interaction in the session.
//
// **Every path exits 0.** See internal/hookio.
package main

import (
	"fmt"
	"os"

	"github.com/brainite/plugin/internal/handlers"
	"github.com/brainite/plugin/internal/hookio"
)

const usage = `brainite-hook <subcommand>

Hook subcommands (stdin: one JSON hook payload):
  session-start   open the spool, emit the cached brief
  prompt          segment on prompt_id, inject the matched skill
  pretool         open a step
  posttool        close a step
  stop            close the segment, resolve the outcome, spawn a flush
  flush           assemble closed segments and POST /runs
  session-end     signal the flusher and return

Local subcommands:
  enable [dir]    opt a repo into capture (default: cwd)
  done [dir]      mark the current task a confirmed success (default: cwd)
  version
`

func main() {
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stdout, usage)
		os.Exit(0)
	}
	sub := os.Args[1]

	switch sub {
	case "session-start":
		hookio.Run(sub, handlers.SessionStart)
	case "prompt":
		hookio.Run(sub, handlers.Prompt)
	case "pretool":
		hookio.Run(sub, handlers.PreTool)
	case "posttool":
		hookio.Run(sub, handlers.PostTool)
	case "stop":
		hookio.Run(sub, handlers.Stop)
	case "flush":
		hookio.Run(sub, handlers.Flush)
	case "session-end":
		hookio.Run(sub, handlers.SessionEnd)
	case "enable":
		dir := ""
		if len(os.Args) > 2 {
			dir = os.Args[2]
		}
		if err := handlers.Enable(dir); err != nil {
			fmt.Fprintln(os.Stdout, "could not enable capture:", err)
			os.Exit(0)
		}
		fmt.Fprintln(os.Stdout, "Brainite capture enabled for this repo.")
		os.Exit(0)
	case "done":
		// The optional dir matters when the caller's shell cwd is not the repo:
		// the session is resolved by matching the spool's recorded cwd, so a
		// mismatch would confirm a concurrent session in another repo.
		doneDir := ""
		if len(os.Args) > 2 {
			doneDir = os.Args[2]
		}
		sessionID, err := handlers.Done(doneDir)
		if err != nil {
			fmt.Fprintln(os.Stdout, "Brainite:", err)
			os.Exit(0)
		}
		fmt.Fprintf(os.Stdout,
			"Brainite: this task is marked a confirmed success (session %s). "+
				"It will be pushed for distillation when the turn ends.\n", sessionID)
		os.Exit(0)
	case "version":
		fmt.Fprintln(os.Stdout, version)
		os.Exit(0)
	default:
		// An unknown subcommand is still exit 0: a stale hooks.json pointing at a
		// removed subcommand must degrade to "captures nothing", never to a hook
		// error the user sees on every prompt.
		hookio.Debugf("unknown subcommand %q", sub)
		os.Exit(0)
	}
}

var version = "0.1.0"
