// What a page shows when it throws while drawing: what happened, the error's text, and what to do (reload, or go back),
// instead of React leaving the window white. Written without JSX so its plain tests run in Node.
import { Component, createElement, type ReactNode } from "react";

type Props = { children?: ReactNode };
type State = { error: unknown };

export class PageErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: unknown): State {
    return { error };
  }

  componentDidCatch(error: unknown) {
    console.error("the page hit an error while drawing", error);
  }

  render() {
    if (this.state.error == null) return this.props.children;
    const error = this.state.error;
    const text = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    return createElement("div", { className: "alert bad page-error", role: "alert" },
      createElement("h2", null, "This page hit an error"),
      createElement("p", null, "Reload the page to start it again, or go back to the page before. If it happens again, "
        + "send the error below with what you did."),
      createElement("pre", { className: "mono" }, text),
      createElement("div", { className: "toolbar" },
        createElement("button", { className: "primary", onClick: () => window.location.reload() }, "Reload the page"),
        createElement("button", { onClick: () => window.history.back() }, "Go back")));
  }
}
