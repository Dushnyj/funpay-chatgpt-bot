import { Link } from 'react-router-dom'
import { Icon } from '../components/Icon'

export default function NotFound() {
  return (
    <div className="not-found">
      <div className="not-found__code">404</div>
      <h1>Страница не найдена</h1>
      <p>Возможно, адрес изменился или раздел ещё не реализован.</p>
      <Link className="button button--primary" to="/"><Icon name="dashboard" />Вернуться к обзору</Link>
    </div>
  )
}
