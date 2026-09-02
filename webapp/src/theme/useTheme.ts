import { useCallback, useEffect, useState } from "react";

export type Theme = "dark" | "light";

function read(): Theme {
  const saved = localStorage.getItem("theme");
  return saved === "light" ? "light" : "dark";
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(read);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
  }, [theme]);

  const toggle = useCallback(
    () => setTheme((t) => (t === "dark" ? "light" : "dark")),
    [],
  );

  return { theme, toggle, setTheme };
}
