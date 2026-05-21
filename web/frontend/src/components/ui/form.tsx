/**
 * Minimal Form primitives.
 *
 * The canonical shadcn Form depends on react-hook-form + @hookform/resolvers,
 * which the plan did not enumerate. To keep the dependency surface tight,
 * this file ships small uncontrolled `<form>` building blocks that match
 * shadcn's class-naming conventions. Downstream agents who need RHF can
 * add it and re-author this file using the canonical shadcn template.
 */

import * as React from "react";

import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

const Form = React.forwardRef<HTMLFormElement, React.FormHTMLAttributes<HTMLFormElement>>(
  ({ className, ...props }, ref) => (
    <form ref={ref} className={cn("space-y-6", className)} {...props} />
  ),
);
Form.displayName = "Form";

const FormItem = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("space-y-2", className)} {...props} />
  ),
);
FormItem.displayName = "FormItem";

const FormLabel = React.forwardRef<
  React.ElementRef<typeof Label>,
  React.ComponentPropsWithoutRef<typeof Label> & { error?: boolean }
>(({ className, error, ...props }, ref) => (
  <Label
    ref={ref}
    className={cn(error && "text-destructive", className)}
    {...props}
  />
));
FormLabel.displayName = "FormLabel";

const FormControl = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn(className)} {...props} />
  ),
);
FormControl.displayName = "FormControl";

const FormDescription = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLParagraphElement>
>(({ className, ...props }, ref) => (
  <p
    ref={ref}
    className={cn("text-sm text-muted-foreground", className)}
    {...props}
  />
));
FormDescription.displayName = "FormDescription";

const FormMessage = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLParagraphElement>
>(({ className, children, ...props }, ref) => {
  if (!children) return null;
  return (
    <p
      ref={ref}
      className={cn("text-sm font-medium text-destructive", className)}
      {...props}
    >
      {children}
    </p>
  );
});
FormMessage.displayName = "FormMessage";

export { Form, FormItem, FormLabel, FormControl, FormDescription, FormMessage };
