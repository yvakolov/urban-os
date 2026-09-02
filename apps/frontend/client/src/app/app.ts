import { ChangeDetectionStrategy, Component } from '@angular/core';
import { ShellLayout } from './shell/shell-layout';

@Component({
  imports: [ShellLayout],
  selector: 'app-root',
  templateUrl: './app.html',
  styleUrl: './app.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class App {}
