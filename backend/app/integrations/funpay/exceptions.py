class FunPayError(Exception):
    """Базовое исключение для всех ошибок FunPay-слоя."""


class GoldenKeyError(FunPayError):
    """golden_key протух или невалиден — требуется перевыпуск."""


class FunPayApiError(FunPayError):
    """HTTP-ошибка вызова FunPay API с сохранённым ответом."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"FunPay API error {status}: {body}")
