import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import sleep
from typing import Callable
from dotenv import load_dotenv
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, TimeoutError, sync_playwright

load_dotenv()

LOGIN_URL = "https://datadriven.datawake.com.br:8057/data-driven/login.html"
DASH_LOAD_SCRIPT = """
loadPageNew('dash.html', 'DASH', 'pageContent',
        'https://datadriven.datawake.com.br:8091/',
        'frameDash', 'OEE-Online');
"""

PAGE_TIMEOUT_MS = 15_000
CLICK_TIMEOUT_MS = 5_000
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
SWITCH_INTERVAL_SECONDS = 70

LOGGER = logging.getLogger("robo_mqb")


@dataclass(frozen=True)
class Credentials:
    nth_1: int
    nth_2: int
    login: str
    senha: str


def setup_logging() -> None:
    if LOGGER.handlers:
        return

    LOGGER.setLevel(logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    LOGGER.addHandler(console_handler)

    try:
        logs_dir = Path(__file__).resolve().parent / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            logs_dir / "robo_mqb.log",
            maxBytes=2_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)
    except OSError as exc:
        LOGGER.warning("Nao foi possivel inicializar log em arquivo local: %s", exc)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"Variavel de ambiente obrigatoria ausente: {name}")
    return value.strip()


def load_credentials() -> Credentials:
    try:
        nth_1 = int(_required_env("NTH_1"))
        nth_2 = int(_required_env("NTH_2"))
    except ValueError as exc:
        raise ValueError("NTH_1 e NTH_2 devem ser inteiros validos.") from exc

    if nth_1 < 0 or nth_2 < 0:
        raise ValueError("NTH_1 e NTH_2 nao podem ser negativos.")

    return Credentials(
        nth_1=nth_1,
        nth_2=nth_2,
        login=_required_env("Login"),
        senha=_required_env("senha"),
    )


def retry(action: Callable[[], None], action_name: str) -> None:
    last_error: Exception | None = None

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            action()
            LOGGER.info("%s executado com sucesso (tentativa %s/%s).", action_name, attempt, RETRY_ATTEMPTS)
            return
        except Exception as exc:  # noqa: BLE001 - erro externo de automacao
            last_error = exc
            LOGGER.warning(
                "Falha em %s (tentativa %s/%s): %s",
                action_name,
                attempt,
                RETRY_ATTEMPTS,
                exc,
            )
            if attempt < RETRY_ATTEMPTS:
                sleep(RETRY_DELAY_SECONDS)

    raise RuntimeError(f"Nao foi possivel concluir: {action_name}") from last_error


def navigate_to_login(page: Page) -> None:
    page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT_MS)
    LOGGER.info("Pagina de login carregada.")


def perform_login(page: Page, credentials: Credentials) -> None:
    page.get_by_role("textbox", name="Email:").fill(credentials.login)
    page.get_by_role("textbox", name="Senha").fill(credentials.senha)
    page.get_by_role("button", name="Login").click(timeout=CLICK_TIMEOUT_MS)
    LOGGER.info("Login enviado.")
    sleep(5)


def open_dash_section(page: Page) -> None:
    retry(
        lambda: page.locator("header i").click(timeout=CLICK_TIMEOUT_MS),
        "Abrir menu principal",
    )
    sleep(1)
    page.get_by_role("link", name=re.compile("DASHBOARD", re.IGNORECASE)).click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_role("link", name=re.compile("MANUFATURA", re.IGNORECASE)).click(timeout=CLICK_TIMEOUT_MS)
    LOGGER.info("Navegacao para Dashboard > Manufatura concluida.")
    page.evaluate(DASH_LOAD_SCRIPT)
    sleep(1)
    retry(
        lambda: page.locator("header i").click(timeout=CLICK_TIMEOUT_MS),
        "Fechar menu principal",
    )


def prepare_iframe(page: Page) -> None:
    iframe = page.frame_locator("#frameDash")
    sleep(10)

    # Alguns botoes do iframe podem nao estar visiveis em todos os estados.
    optional_actions: list[tuple[str, Callable[[], None], int]] = [
        (
            "Atualizar dados do iframe",
            lambda: iframe.locator("button:has(svg.animate-spin)").click(timeout=CLICK_TIMEOUT_MS),
            3,
        ),
        (
            "Ativar modo tela cheia",
            lambda: iframe.locator("button:has-text('Modo Tela Cheia')").click(timeout=CLICK_TIMEOUT_MS),
            2,
        ),
        (
            "Fechar modal auxiliar",
            lambda: iframe.locator("button:has(svg.lucide-x)").click(timeout=CLICK_TIMEOUT_MS),
            3,
        ),
    ]

    for action_name, action, wait_after in optional_actions:
        try:
            action()
            LOGGER.info("%s concluido.", action_name)
            sleep(wait_after)
        except Exception as exc:  # noqa: BLE001 - erro externo de automacao
            LOGGER.warning("%s nao aplicado: %s", action_name, exc)


def interagir_com_dashboard(page: Page, nth_index: int) -> None:
    iframe = page.frame_locator("#frameDash")
    detalhes = iframe.locator("button:has-text('Detalhes')")
    detalhes.first.wait_for(timeout=CLICK_TIMEOUT_MS)

    total_disponivel = detalhes.count()
    if nth_index >= total_disponivel:
        raise IndexError(f"NTH={nth_index} fora do intervalo. Total disponivel: {total_disponivel}")

    retry(
        lambda: detalhes.nth(nth_index).click(timeout=CLICK_TIMEOUT_MS),
        f"Abrir dashboard NTH={nth_index}",
    )
    sleep(2)


def voltar_para_lista(page: Page) -> None:
    iframe = page.frame_locator("#frameDash")
    retry(
        lambda: iframe.locator("a").get_by_role("button").click(timeout=CLICK_TIMEOUT_MS),
        "Voltar para lista de dashboards",
    )
    sleep(5)


def cycle_dashboards(page: Page, credentials: Credentials) -> None:
    current_nth = credentials.nth_1
    interagir_com_dashboard(page, current_nth)

    while True:
        LOGGER.info(
            "Dashboard atual: NTH=%s. Proxima mudanca em %s segundos.",
            current_nth,
            SWITCH_INTERVAL_SECONDS,
        )
        sleep(SWITCH_INTERVAL_SECONDS)
        voltar_para_lista(page)
        current_nth = credentials.nth_2 if current_nth == credentials.nth_1 else credentials.nth_1
        LOGGER.info("Mudando para NTH=%s.", current_nth)
        interagir_com_dashboard(page, current_nth)


def run(playwright: Playwright) -> None:
    setup_logging()
    LOGGER.info("Iniciando navegador as %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    try:
        credentials = load_credentials()
    except ValueError as exc:
        LOGGER.error("Erro de configuracao: %s", exc)
        return

    browser: Browser | None = None
    context: BrowserContext | None = None

    try:
        browser = playwright.chromium.launch(headless=False, args=["--start-maximized"])
        context = browser.new_context(no_viewport=True)
        page = context.new_page()

        navigate_to_login(page)
        perform_login(page, credentials)
        open_dash_section(page)
        prepare_iframe(page)
        cycle_dashboards(page, credentials)
    except TimeoutError as exc:
        LOGGER.error("Timeout durante a automacao: %s", exc)
    except KeyboardInterrupt:
        LOGGER.info("Execucao interrompida manualmente.")
    except Exception as exc:  # noqa: BLE001 - garante log de qualquer erro nao previsto
        LOGGER.exception("Erro inesperado durante a automacao: %s", exc)
    finally:
        if context is not None:
            try:
                context.close()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Falha ao fechar contexto do navegador: %s", exc)

        if browser is not None:
            try:
                browser.close()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Falha ao fechar navegador: %s", exc)


if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)
