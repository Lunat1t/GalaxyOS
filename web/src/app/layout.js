import "./globals.css";

export const metadata = {
  title: "Galaxy Code",
  description: "Локальная рабочая область проектов и задач",
};

export default function RootLayout({ children }) {
  return (
    <html lang="ru" className="dark">
      <body>{children}</body>
    </html>
  );
}
