import { createRoot } from "react-dom/client";
import { Options } from "./Options";

const container = document.getElementById("root");
if (container) {
  createRoot(container).render(<Options />);
}
