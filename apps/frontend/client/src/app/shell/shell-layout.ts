import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { HlmSeparatorImports } from '@spartan-ng/helm/separator';

/**
 * Оболочка приложения: шапка с меню и место под страницу.
 *
 * Живёт отдельным компонентом, а не в App, чтобы шапка не перемонтировалась
 * при переходах: меняется только содержимое router-outlet.
 */
@Component({
  selector: 'app-shell-layout',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, HlmSeparatorImports],
  templateUrl: './shell-layout.html',
  styleUrl: './shell-layout.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ShellLayout {}
