import { BearerForm } from "./BearerForm";
import { LiveStream } from "./LiveStream";
import { IgnoreList } from "./IgnoreList";
import { Counts } from "./Counts";
import { IdentityLabel } from "./IdentityLabel";

export function App() {
  return (
    <div className="app">
      <h1>Fulcra Attention</h1>
      <BearerForm />
      <Counts />
      <LiveStream />
      <IgnoreList />
      <IdentityLabel />
    </div>
  );
}
