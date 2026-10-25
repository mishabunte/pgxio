import { Pgxio } from "./index.ts";
export type Task = { id: string; title: string; done: boolean };
export type TasksAPI = {
  list_tasks: { input: Record<string, never>; output: { tasks: Task[] } };
  create_task: { input: { title: string }; output: Task };
};
export type TasksStreams = { tasks: { input: Record<string, never> } };
export const tasksClient = (url: string, token: () => string | Promise<string>) => new Pgxio<TasksAPI, TasksStreams>(url, token);
