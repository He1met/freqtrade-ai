import { useEffect, useState } from "react";

import type { MvpData } from "../../api/types";
import {
  EMPTY_LAB_SELECTION,
  reconcileLabSelection,
  selectLabEntity,
  type LabSelection,
} from "./candidateWorkbenchModel";

export function useLabSelection(data: MvpData) {
  const [selection, setSelection] = useState<LabSelection>(EMPTY_LAB_SELECTION);

  useEffect(() => {
    setSelection((current) => reconcileLabSelection(data, current));
  }, [data]);

  function select(key: keyof LabSelection, value: string | null) {
    setSelection((current) => reconcileLabSelection(data, selectLabEntity(current, key, value)));
  }

  return { selection, select };
}
