import { createRoot } from "react-dom/client";
import { Wizard } from "./Wizard";

const container = document.getElementById("root");
if (container) {
  createRoot(container).render(<Wizard />);
}
