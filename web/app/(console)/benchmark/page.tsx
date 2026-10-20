import { BenchmarkScreen } from "@/components/console/benchmark-screen";
import { getBenchmark } from "@/lib/api";

export default async function BenchmarkPage() {
  const { rows, note, perRepo, argusEmpty, gate } = await getBenchmark();
  return (
    <BenchmarkScreen
      rows={rows}
      note={note}
      perRepo={perRepo}
      argusEmpty={argusEmpty}
      gate={gate}
    />
  );
}
